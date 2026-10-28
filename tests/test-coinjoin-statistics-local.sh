#!/usr/bin/env bash
# Run the rendered CoinJoin Statistics PBS job on this host: the CRoCS daily workflow of
# coinjoin-analysis over a Dumplings snapshot, with Docker in place of Singularity and a
# directory in place of S3 (tests/support/persistent-index). The snapshot merges the
# coinjoin-analysis test samples of Wasabi 2, Wasabi 1 and JoinMarket.
#
# Needs Docker, the coinjoin-analysis image (COINJOIN_ANALYSIS_IMAGE, default
# ghcr.io/ondrejman/coinjoin-analysis:latest) and the sibling coinjoin-analysis checkout.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
support="$repo/tests/support/persistent-index"
fixtures=${COINJOIN_ANALYSIS_FIXTURES:-$repo/../coinjoin-analysis/tests/fixtures}
export FAKE_SINGULARITY_IMAGE=${COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}
work=$(mktemp -d)
export FAKE_S3="$work/s3" PATH="$support:$PATH"
# Keep the job output for diagnosis when a check fails.
trap 'status=$?; if [ "$status" = 0 ]; then rm -rf "$work"; else echo "kept $work"; fi' EXIT

snapshot="$FAKE_S3/bucket/dumplings/generations/g1"
mkdir -p "$snapshot" "$work/scratch" "$work/unpack"
for sample in dumplings__end_zksnacks_202405 dumplings__ww1_202405 dumplings__jm_202406; do
    unzip -qo "$fixtures/$sample.zip" 'Scanner/*' -d "$work/unpack"
done
cp "$work/unpack/Scanner/"*.txt "$snapshot/"
echo 870000 >"$snapshot/LastProcessedBlockHeight.txt"
touch "$work/credentials"

(cd "$repo" && uv run --frozen python "$support/render_job.py" statistics --run-id stats-1 --root "$work" \
    --input-uri s3://bucket/dumplings/generations/g1 --protocol wasabi2 --protocol wasabi1 \
    --protocol joinmarket --credentials "$work/credentials" --output "$work/statistics.pbs")
SCRATCHDIR="$work/scratch" PBS_JOBID=statistics.local bash "$work/statistics.pbs" >"$work/statistics.out" 2>&1 \
    || { grep '^\[statistics\]' "$work/statistics.out" | tail -5; tail -30 "$work/statistics.out"; echo FAIL; exit 1; }

result=$(find "$FAKE_S3/bucket/runs/stats-1/coinjoin-analysis-tools_data/coinjoin_statistics" -mindepth 1 \
    -maxdepth 1 -type d | head -1)
site="$result/site"
for plot in wasabi2/wasabi2_cummul_values_norm.png wasabi1/wasabi1_cummul_values_norm.png \
    joinmarket_all/joinmarket_all_cummul_values_norm.png; do
    test -s "$site/$plot" || { echo "FAIL: no $plot"; exit 1; }
done
echo "OK: cumulative liquidity plots of all three protocols"
# Wasabi 1 liquidity covers its zkSNACKs and other pools, not the whole protocol.
for pool in wasabi2 wasabi1_zksnacks joinmarket_all; do
    test -s "$site/liquidity_summary_$pool.json" || { echo "FAIL: no liquidity summary for $pool"; exit 1; }
    test -s "$site/texts/$pool.html" || { echo "FAIL: no liquidity fragment for $pool"; exit 1; }
done
echo "OK: liquidity summaries and page texts"
[ -n "$(find "$site" -path '*/20*' -name '*_input_types_values_norm.png' -print -quit)" ] \
    || { echo "FAIL: no interval plots"; exit 1; }
echo "OK: interval plots"
grep -q '"lastProcessedBlockHeight": "870000"' "$site/summary.json" || { echo "FAIL: no summary.json"; exit 1; }
echo "OK: summary.json records the Dumplings height"
if [ -n "$(find "$result" -name 'coinjoin_tx_info.json' -print -quit)" ] || [ -n "$(ls -A "$result/input")" ]; then
    echo "FAIL: processed data or the snapshot copy was published"; exit 1
fi
echo "OK: only the site, logs and invocation were published"
test -s "$result/analysis.log" || { echo "FAIL: no analysis log"; exit 1; }
test -f "$FAKE_S3/bucket/runs/stats-1/.pbs/coinjoin-analysis.done" || { echo "FAIL: no done marker"; exit 1; }
echo "OK: log and done marker published"
echo PASS
