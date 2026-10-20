#!/bin/bash
#PBS -N coinjoin_analysis_dumplings_s3
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_{scratch_type}={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
set -euo pipefail

{stage_header}
IMAGE={image}
DUMPLINGS_URI={dumplings_uri}
CJTYPE={cjtype}
MIX_ID={mix_id}
FALSE_CJTXS={false_cjtxs}
OUTPUT="$RUN_WORK/coinjoin-analysis_data"
mkdir -p "$RUN_WORK/.pbs" "$RUN_WORK/logs" "$OUTPUT"
stage_finalize() {{
  :
}}
{bootstrap}
{stage_setup}
echo "[coinjoin-analysis] reading the latest Dumplings snapshot from $DUMPLINGS_URI"
{download_latest}
SNAPSHOT_PREFIX="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["prefix"])' "$RUN_WORK/dumplings-latest.json")"
SNAPSHOT_HEIGHT="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["last_processed_height"])' "$RUN_WORK/dumplings-latest.json")"
echo "[coinjoin-analysis] snapshot $SNAPSHOT_PREFIX at height $SNAPSHOT_HEIGHT"
# Parent of Scanner/: parse_dumplings also writes summary.log and operation.txt one level up.
WORK="$SCRATCHDIR/dumplings-work"
mkdir -p "$WORK/dumplings/Scanner"
{download_snapshot}
# coinjoin-analysis reads <file>.json instead of a Dumplings file when one exists and never
# checks its age, so the snapshot must arrive without them.
find "$WORK/dumplings/Scanner" -maxdepth 1 -name '*.txt.json' -delete
singularity exec \
  --cleanenv \
  --bind "$WORK:/work:rw" \
  --env MPLCONFIGDIR=/tmp \
  "$IMAGE" \
  bash -c 'cd /work && {command}'
test -f "$WORK/dumplings/Scanner/$MIX_ID/coinjoin_tx_info.json" || {{
  echo "coinjoin-analysis did not produce Scanner/$MIX_ID/coinjoin_tx_info.json" >&2
  exit 1
}}
cp "$WORK/dumplings/Scanner/$MIX_ID/coinjoin_tx_info.json" "$OUTPUT/coinjoin_tx_info.json"
# The curated false positives shipped with this coinjoin-analysis image, for the comparison.
singularity exec --cleanenv "$IMAGE" cat "/opt/coinjoin-analysis/data/$FALSE_CJTXS/false_cjtxs.json" \
  > "$OUTPUT/false_cjtxs.json" || {{ echo "[coinjoin-analysis] the image has no curated false_cjtxs.json" >&2; rm -f "$OUTPUT/false_cjtxs.json"; }}
cp "$RUN_WORK/dumplings-latest.json" "$OUTPUT/dumplings-snapshot.json"
echo "[coinjoin-analysis] baseline of $(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["coinjoins"]))' "$OUTPUT/coinjoin_tx_info.json") coinjoins through height $SNAPSHOT_HEIGHT"
{upload_results}
