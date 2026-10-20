#!/usr/bin/env bash
# Run the rendered Dumplings-baseline PBS job on this host: coinjoin-analysis over the
# snapshot that latest.json names, with Docker in place of Singularity and a directory
# in place of S3 (tests/support/persistent-index). The snapshot is the JoinMarket
# sample in temp_dumplings/Scanner plus a stale `.txt.json` cache the job must drop.
#
# Needs Docker and the coinjoin-analysis image (COINJOIN_ANALYSIS_IMAGE, default
# coinjoin-analysis:local).
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
support="$repo/tests/support/persistent-index"
sample=${DUMPLINGS_SAMPLE:-$repo/../temp_dumplings/Scanner}
export FAKE_SINGULARITY_IMAGE=${COINJOIN_ANALYSIS_IMAGE:-coinjoin-analysis:local}
work=$(mktemp -d)
export FAKE_S3="$work/s3" PATH="$support:$PATH"
trap 'rm -rf "$work"' EXIT

snapshot="$FAKE_S3/bucket/dumplings/392400"
mkdir -p "$snapshot" "$work/scratch"
cp "$sample/OtherCoinJoins.txt" "$sample/OtherCoinJoinPostMixTxs.txt" "$snapshot/"
echo 392400 >"$snapshot/LastProcessedBlockHeight.txt"
# A stale coinjoin-analysis cache: read instead of OtherCoinJoins.txt if it survived.
python3 -I -c 'import pickle, sys; pickle.dump({}, open(sys.argv[1], "wb"))' "$snapshot/OtherCoinJoins.txt.json"
cat >"$FAKE_S3/bucket/dumplings/latest.json" <<JSON
{"last_processed_height": 392400, "prefix": "s3://bucket/dumplings/392400", "dumplings_commit": "test"}
JSON
touch "$work/credentials"

(cd "$repo" && uv run --frozen python "$support/render_job.py" dumplings --run-id baseline-1 --root "$work" \
    --dumplings-uri s3://bucket/dumplings --coinjoin-type joinmarket --credentials "$work/credentials" \
    --output "$work/baseline.pbs")
SCRATCHDIR="$work/scratch" PBS_JOBID=baseline.local bash "$work/baseline.pbs" >"$work/baseline.out" 2>&1 \
    || { tail -30 "$work/baseline.out"; echo FAIL; exit 1; }

out="$FAKE_S3/bucket/runs/baseline-1/coinjoin-analysis_data"
count=$(python3 -I -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["coinjoins"]))' "$out/coinjoin_tx_info.json")
[ "$count" -gt 0 ] || { echo "FAIL: empty baseline (the stale cache was used)"; exit 1; }
echo "OK: baseline holds $count JoinMarket coinjoins"
test -s "$out/false_cjtxs.json" || { echo "FAIL: no curated false_cjtxs.json"; exit 1; }
echo "OK: curated false_cjtxs.json shipped with the baseline"
grep -q '"last_processed_height": 392400' "$out/dumplings-snapshot.json" || { echo "FAIL: no snapshot record"; exit 1; }
echo "OK: snapshot provenance recorded"
test -f "$FAKE_S3/bucket/runs/baseline-1/.pbs/coinjoin-analysis.done" || { echo "FAIL: no done marker"; exit 1; }
echo "OK: done marker published"
echo PASS
