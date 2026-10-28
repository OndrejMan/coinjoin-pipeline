#!/usr/bin/env bash
# Run the rendered persistent-index PBS jobs on this host (plan §2.6, §2.11).
#
# singularity and s5cmd are replaced by Docker and a directory (tests/support/
# persistent-index), the chain is regtest, and the "multijob" working copy is a
# temporary directory. The jobs run in the order a mainnet index sees: parse,
# an S3 backup, a pinned script analysis, an update, then an update after a
# file went missing on the working copy, which must restore it from the backup,
# and a second backup that replaces the first.
#
# Needs Docker, zstd, bitcoin/bitcoin:29.1-alpine and the BlockSci image
# (BLOCKSCI_IMAGE, default blocksci-complete:local).
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
support="$repo/tests/support/persistent-index"
export FAKE_SINGULARITY_IMAGE=${BLOCKSCI_IMAGE:-blocksci-complete:local}
work=$(mktemp -d)
core=persistent-index-core-$$
export FAKE_S3="$work/s3" PATH="$support:$PATH"
index="$work/multijob/blocksci-mainnet"
backup_root="$FAKE_S3/bucket/blocksci-index"
backup="$backup_root"
mkdir -p "$work/btc" "$FAKE_S3/bucket/runs" "$work/multijob"
touch "$work/credentials"

cleanup() {
    docker rm -f "$core" >/dev/null 2>&1 || true
    if [[ "${KEEP_TEST_WORK:-0}" == 1 ]]; then
        echo "Kept persistent-index evidence: $work"
    else
        rm -rf "$work"
    fi
}
trap cleanup EXIT

cli() {
    docker exec -u bitcoin "$core" bitcoin-cli -regtest -datadir=/home/bitcoin/.bitcoin "$@"
}

mine() {
    local address
    address=$(cli getnewaddress)
    for _ in $(seq "$1"); do
        cli sendtoaddress "$(cli getnewaddress)" 0.5 >/dev/null
        cli generatetoaddress 1 "$address" >/dev/null
    done
}

# Render and run one job as PBS would: fresh SCRATCHDIR, exporters staged in "S3".
job() {
    local kind=$1 run=$2
    shift 2
    mkdir -p "$FAKE_S3/bucket/runs/$run/.pipeline/exporters" "$work/scratch-$run"
    cp -r "$repo/pipeline/exporters/." "$FAKE_S3/bucket/runs/$run/.pipeline/exporters/"
    (cd "$repo" && uv run --frozen python "$support/render_job.py" "$kind" --run-id "$run" --root "$work" \
        --index-dir "$index" --backup-uri s3://bucket/blocksci-index --credentials "$work/credentials" \
        --output "$work/$run.pbs" "$@")
    SCRATCHDIR="$work/scratch-$run" PBS_JOBID="$run.local" bash "$work/$run.pbs" >"$work/$run.out" 2>&1 \
        || { echo "FAIL: $run"; tail -30 "$work/$run.out"; [ -f "$FAKE_S3/bucket/runs/$run/logs/blocksci-parse.pbs.log" ] && tail -30 "$FAKE_S3/bucket/runs/$run/logs/blocksci-parse.pbs.log"; exit 1; }
}

height() {
    sed -nE 's/.*"exported_max_block"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' "$1/manifest.json"
}

expect() {
    if [ "$2" != "$3" ]; then
        echo "FAIL: $1: expected $3, got $2"
        exit 1
    fi
    echo "OK: $1 = $2"
}

docker run -d --name "$core" -e UID="$(id -u)" -e GID="$(id -g)" -v "$work/btc":/home/bitcoin/.bitcoin \
    bitcoin/bitcoin:29.1-alpine -regtest -server -blocksxor=0 -fallbackfee=0.0001 >/dev/null
for _ in $(seq 60); do cli getblockcount >/dev/null 2>&1 && break; sleep 1; done
cli createwallet persistent >/dev/null
cli generatetoaddress 120 "$(cli getnewaddress)" >/dev/null
mine 20
first=$(cli getblockcount)

job parse index-parse --bitcoin-datadir "$work/btc/regtest" --max-block "$first"
expect "index height after parse" "$(height "$index")" "$first"
expect "index height published to S3" \
    "$(sed -nE 's/.*"exported_max_block"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' "$FAKE_S3/bucket/runs/index-parse/blocksci-index-manifest.json")" "$first"

job backup index-backup-1
expect "backup height" "$(height "$backup")" "$first"
(cd "$backup" && sha256sum -c blocksci_index.tar.zst.sha256 >/dev/null) || { echo "FAIL: backup checksum"; exit 1; }
echo "OK: the backup matches its SHA-256"
expect "backup height recorded in the run" \
    "$(sed -nE 's/.*"exported_max_block"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' "$FAKE_S3/bucket/runs/index-backup-1/blocksci-index-backup.json")" "$first"

job script scan-1 --script "$repo/scripts/metacentrum_mainnet_coinjoin_scan.py" --pin-height "$first"
summary="$FAKE_S3/bucket/runs/scan-1/blocksci-custom-analysis_data/joinmarket-mainnet-summary.json"
scan_one=$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["status"], d["index"]["last_height"], d["index"]["last_block_hash"])' "$summary")
expect "pinned scan status and height" "$(cut -d' ' -f1,2 <<<"$scan_one")" "complete $first"
test -f "$FAKE_S3/bucket/runs/scan-1/blocksci-custom-analysis_data/blocksci-index-manifest.json"
echo "OK: the analysis recorded the index manifest it read"

mine 15
second=$(cli getblockcount)
job update index-update-1 --bitcoin-datadir "$work/btc/regtest" --max-block "$second"
expect "index height after update" "$(height "$index")" "$second"
expect "backup height after an update" "$(height "$backup")" "$first"

# Multijob scratch may purge files it considers unused.
rm -f "$index/parsed/chain/block.dat"
mine 5
third=$(cli getblockcount)
job update index-update-2 --bitcoin-datadir "$work/btc/regtest" --max-block "$third"
grep -q "restoring the working copy" "$work/index-update-2.out" || { echo "FAIL: no restore"; exit 1; }
echo "OK: the update restored the purged working copy from the S3 backup"
expect "index height after restore and update" "$(height "$index")" "$third"
job backup index-backup-2
expect "backup height after the second backup" "$(height "$backup")" "$third"
expect "objects in the backup prefix" "$(ls "$backup" | tr '\n' ' ')" "blocksci_index.tar.zst blocksci_index.tar.zst.sha256 manifest.json "
echo "OK: the second backup replaced the first"

job script scan-2 --script "$repo/scripts/metacentrum_mainnet_coinjoin_scan.py" --pin-height "$first"
scan_two=$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["status"], d["index"]["last_height"], d["index"]["last_block_hash"])' "$FAKE_S3/bucket/runs/scan-2/blocksci-custom-analysis_data/joinmarket-mainnet-summary.json")
expect "pinned scan after two updates" "$scan_two" "$scan_one"
# The comparison over the same pinned index, against a baseline with one coinjoin-looking
# transaction of the chain (BlockSci finds no Wasabi 2 rounds on regtest).
baseline_dir="$FAKE_S3/bucket/runs/baseline-1/coinjoin-analysis_data"
mkdir -p "$baseline_dir"
txid=$(cli getblock "$(cli getblockhash 130)" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["tx"][1])')
printf '{"coinjoins": {"%s": {"txid": "%s", "broadcast_time": "2026-10-19 00:00:00.000", "inputs": {}, "outputs": {}}}}' \
    "$txid" "$txid" >"$baseline_dir/coinjoin_tx_info.json"
job compare compare-1 --pin-height "$first" --baseline-uri s3://bucket/runs/baseline-1/coinjoin-analysis_data/coinjoin_tx_info.json
coverage=$(python3 -I -c 'import json,sys; c=json.load(open(sys.argv[1]))["coverage"]; print(c["index_blocks"], c["baseline_in_window"], c["baseline_total"])' \
    "$FAKE_S3/bucket/runs/compare-1/coinjoinPipeline_data/unified_report.json")
expect "comparison coverage (index blocks, baseline in window, total)" "$coverage" "$((first + 1)) 1 1"
test -f "$FAKE_S3/bucket/runs/compare-1/coinjoinPipeline_data/blocksci-index-manifest.json"
echo "OK: the comparison recorded the index manifest it read"

[ ! -e "$index/.lock/writer" ] && [ -z "$(ls -A "$index/.lock/readers")" ] || { echo "FAIL: a lock was left behind"; exit 1; }
echo "OK: no lock left behind"
echo PASS
