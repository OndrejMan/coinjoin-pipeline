#!/usr/bin/env bash
# Proof of concept for the incrementally updated mainnet index (scenario-lab/mainnet-analysis-plan-v2.md §2.4, §2.6).
#
# On a small regtest chain it checks that
#   1. `blocksci_parser update` extends an index in place,
#   2. an analysis opened with `Blockchain(config, H)` sees the same chain before
#      and after that update, so analyses can pin a height instead of a copy,
#   3. a primary copy can follow the working copy with rsync,
# and reports which index files the update only appends to and which it rewrites.
#
# Needs Docker, rsync, the BlockSci image (BLOCKSCI_IMAGE, default
# blocksci-complete:local) and bitcoin/bitcoin:29.1-alpine. Touches nothing
# outside a temporary directory.
set -euo pipefail

here=$(dirname "$(realpath "$0")")
image=${BLOCKSCI_IMAGE:-blocksci-complete:local}
core_image=bitcoin/bitcoin:29.1-alpine
core=blocksci-pinning-core-$$
work=$(mktemp -d)
mkdir -p "$work/btc" "$work/index"

cleanup() {
    docker rm -f "$core" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

cli() {
    docker exec -u bitcoin "$core" bitcoin-cli -regtest -datadir=/home/bitcoin/.bitcoin "$@"
}

# The image's Python lives under /root, so commands run as root and hand the
# files back to the caller for rsync from the host.
blocksci() {
    docker run --rm -w /tmp -v "$work/btc":/btc:ro -v "$work":/work \
        -v "$here/blocksci_pinning_summary.py":/opt/pinning/summary.py:ro "$image" \
        sh -c '"$@"; status=$?; chown -R '"$(id -u):$(id -g)"' /work/index /work/primary 2>/dev/null; exit $status' \
        sh "$@"
}

summary() {
    blocksci python3 /opt/pinning/summary.py "$@"
}

mine() {
    local blocks=$1 address
    address=$(cli getnewaddress)
    for _ in $(seq "$blocks"); do
        cli sendtoaddress "$(cli getnewaddress)" 0.5 >/dev/null
        cli sendtoaddress "$(cli getnewaddress)" 0.25 >/dev/null
        cli generatetoaddress 1 "$address" >/dev/null
    done
}

# -blocksxor=0 keeps the block files readable for BlockSci, as on the archive VM.
docker run -d --name "$core" -e UID="$(id -u)" -e GID="$(id -g)" -v "$work/btc":/home/bitcoin/.bitcoin \
    "$core_image" -regtest -server -blocksxor=0 -fallbackfee=0.0001 -txindex=1 >/dev/null
for _ in $(seq 60); do cli getblockcount >/dev/null 2>&1 && break; sleep 1; done
cli createwallet pinning >/dev/null
cli generatetoaddress 120 "$(cli getnewaddress)" >/dev/null
mine 20
first_tip=$(cli getblockcount)

blocksci blocksci_parser /work/index/config.json generate-config bitcoin_regtest /work/index/parsed \
    --disk /btc/regtest --max-block "$first_tip" >/dev/null
blocksci blocksci_parser /work/index/config.json update >/dev/null
before=$(summary /work/index/config.json)
pin=$(python3 -I -c 'import json, sys; print(json.loads(sys.argv[1])["blocks"])' "$before")
echo "after first parse:      $before"

# The primary copy (/storage in production) starts as a copy of the working one.
cp -a "$work/index" "$work/primary"

# The wallet spends old coinbase outputs, so blocks above the pin spend outputs below it.
mine 15
second_tip=$(cli getblockcount)
sed -i -E "s/(\"maxBlockNum\"[[:space:]]*:[[:space:]]*)[0-9]+/\\1$second_tip/" "$work/index/config.json"
blocksci blocksci_parser /work/index/config.json update >/dev/null
after=$(summary /work/index/config.json)
pinned=$(summary /work/index/config.json --pin "$pin")
unpinned=$(summary /work/index/config.json --scope "$pin")
echo "after update:           $after"
echo "pinned at $pin:          $pinned"
echo "unpinned, first $pin:    $unpinned"

failed=0
python3 -I - "$before" "$after" "$pinned" "$unpinned" <<'PY' || failed=1
import json
import sys

before, after, pinned, unpinned = (json.loads(text) for text in sys.argv[1:])
stable = ("blocks", "txes", "last_block")
if after["blocks"] <= before["blocks"]:
    print("FAIL: the update added no blocks")
    raise SystemExit(1)
if any(before[key] != pinned[key] for key in stable):
    print("FAIL: the pinned chain changed after the in-place update")
    raise SystemExit(1)
print(f"OK: pinned chain unchanged ({pinned['blocks']} blocks, {pinned['txes']} txes)")
later = unpinned["spent_later"]
if pinned["spent_outputs"] == before["spent_outputs"]:
    print(f"OK: the pinned view hides {later} spends of its outputs by blocks above the pin")
else:
    print(f"NOTE: the pinned view shows {pinned['spent_outputs'] - before['spent_outputs']} "
          f"spends by blocks above the pin (unpinned: {later})")
PY

# Which files did the update only append to?
python3 -I - "$work/primary" "$work/index" <<'PY'
import sys
from pathlib import Path

old_root, new_root = Path(sys.argv[1]), Path(sys.argv[2])
kinds = {"unchanged": [], "appended": [], "rewritten": [], "new": [], "deleted": []}
for new in sorted(p for p in new_root.rglob("*") if p.is_file()):
    relative = new.relative_to(new_root)
    old = old_root / relative
    if not old.exists():
        kinds["new"].append(relative)
        continue
    old_bytes, new_bytes = old.read_bytes(), new.read_bytes()
    if old_bytes == new_bytes:
        kinds["unchanged"].append(relative)
    elif new_bytes.startswith(old_bytes):
        kinds["appended"].append(relative)
    else:
        kinds["rewritten"].append(relative)
for old in sorted(p for p in old_root.rglob("*") if p.is_file()):
    if not (new_root / old.relative_to(old_root)).exists():
        kinds["deleted"].append(old.relative_to(old_root))
for kind, paths in kinds.items():
    print(f"{kind:>10}: {len(paths)}")
    if kind in ("appended", "rewritten"):
        for path in paths:
            print(f"{'':>12}{path}")
PY

# Bring the primary copy up to date. --no-whole-file forces the delta
# algorithm a network copy would use; the stats show how much was sent.
rsync -a --delete --no-whole-file --stats "$work/index/" "$work/primary/" \
    | grep -E "^(Number of regular files transferred|Total transferred file size|Literal data|Matched data)"
synced=$(summary /work/primary/config.json --pin "$second_tip" 2>/dev/null || true)
if [ -z "$synced" ]; then
    # config.json names the working directory; open the copy through its own config.
    sed -i "s#/work/index/#/work/primary/#g" "$work/primary/config.json"
    synced=$(summary /work/primary/config.json)
fi
echo "primary after rsync:    $synced"
if [ "$(python3 -I -c 'import json,sys; a,b=(json.loads(x) for x in sys.argv[1:]); print(a["last_block"]==b["last_block"] and a["txes"]==b["txes"])' "$after" "$synced")" != True ]; then
    echo "FAIL: the primary copy differs from the working copy after rsync"
    failed=1
fi

# The mainnet scan script runs in the same image (Python 3.8) against the pinned index.
for kind in wasabi2 joinmarket; do
    docker run --rm -w /tmp -v "$work":/work -v "$here/../scripts/metacentrum_mainnet_coinjoin_scan.py":/opt/scan/scan.py:ro \
        "$image" python3 /opt/scan/scan.py --config /work/index/config.json --output "/tmp/$kind.json" \
        --coinjoin-type "$kind" --min-input-count 15 --max-block "$pin" >/dev/null 2>&1 \
        && echo "OK: mainnet scan ($kind) runs pinned at $pin" \
        || { echo "FAIL: mainnet scan ($kind) failed in the BlockSci image"; failed=1; }
done

[ "$failed" = 0 ] && echo PASS
exit "$failed"
