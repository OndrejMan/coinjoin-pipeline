{preamble}
DUMPLINGS_URI={dumplings_uri}
CJTYPE={cjtype}
MIX_ID={mix_id}
FALSE_CJTXS={false_cjtxs}
OUTPUT="$RUN_WORK/coinjoin-analysis_data"
mkdir -p "$OUTPUT"
{start}
echo "[coinjoin-analysis] reading the latest Dumplings snapshot from $DUMPLINGS_URI"
s5 cp "$DUMPLINGS_URI/latest.json" "$RUN_WORK/dumplings-latest.json"
SNAPSHOT_PREFIX="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["prefix"])' "$RUN_WORK/dumplings-latest.json")"
SNAPSHOT_HEIGHT="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["last_processed_height"])' "$RUN_WORK/dumplings-latest.json")"
echo "[coinjoin-analysis] snapshot $SNAPSHOT_PREFIX at height $SNAPSHOT_HEIGHT"
# Parent of Scanner/: parse_dumplings also writes summary.log and operation.txt one level up.
WORK="$SCRATCHDIR/dumplings-work"
mkdir -p "$WORK/dumplings/Scanner"
s5 cp "$SNAPSHOT_PREFIX/*" "$WORK/dumplings/Scanner/"
# coinjoin-analysis reads <file>.json instead of a Dumplings file when one exists and never
# checks its age, so the snapshot must arrive without them.
find "$WORK/dumplings/Scanner" -maxdepth 1 -name '*.txt.json' -delete
singularity exec \
  --cleanenv \
  --bind "$WORK:/work:rw" \
  --env MPLCONFIGDIR=/tmp \
  "$IMAGE" \
  bash -c 'cd /work && {command}'
require_file "$WORK/dumplings/Scanner/$MIX_ID/coinjoin_tx_info.json" \
  "coinjoin-analysis did not produce Scanner/$MIX_ID/coinjoin_tx_info.json"
cp "$WORK/dumplings/Scanner/$MIX_ID/coinjoin_tx_info.json" "$OUTPUT/coinjoin_tx_info.json"
# The curated false positives shipped with this coinjoin-analysis image, for the comparison.
singularity exec --cleanenv "$IMAGE" cat "/opt/coinjoin-analysis/data/$FALSE_CJTXS/false_cjtxs.json" \
  > "$OUTPUT/false_cjtxs.json" || {{ echo "[coinjoin-analysis] the image has no curated false_cjtxs.json" >&2; rm -f "$OUTPUT/false_cjtxs.json"; }}
cp "$RUN_WORK/dumplings-latest.json" "$OUTPUT/dumplings-snapshot.json"
echo "[coinjoin-analysis] baseline of $(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["coinjoins"]))' "$OUTPUT/coinjoin_tx_info.json") coinjoins through height $SNAPSHOT_HEIGHT"
s5 sync "$OUTPUT/" "$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/"
