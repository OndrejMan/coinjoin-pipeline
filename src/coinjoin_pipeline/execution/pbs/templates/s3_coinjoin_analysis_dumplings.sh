{preamble}
DUMPLINGS_URI={dumplings_uri}
CJTYPE={cjtype}
MIX_ID={mix_id}
FALSE_CJTXS={false_cjtxs}
OUTPUT="$RUN_WORK/coinjoin-analysis_data"
mkdir -p "$OUTPUT"
{start}
echo "[coinjoin-analysis] reading the Dumplings copy from $DUMPLINGS_URI"
# Without manifest.json the copy is being replaced or its upload failed.
s5 cp "$DUMPLINGS_URI/manifest.json" "$RUN_WORK/dumplings-manifest.json"
SNAPSHOT_HEIGHT="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["last_processed_height"])' "$RUN_WORK/dumplings-manifest.json")"
echo "[coinjoin-analysis] Dumplings Scanner/ at height $SNAPSHOT_HEIGHT"
# Parent of Scanner/: parse_dumplings also writes summary.log and operation.txt one level up.
WORK="$SCRATCHDIR/dumplings-work"
mkdir -p "$WORK/dumplings/Scanner"
s5 cp "$DUMPLINGS_URI/Scanner/*" "$WORK/dumplings/Scanner/"
python3 - "$RUN_WORK/dumplings-manifest.json" "$WORK/dumplings/Scanner" <<'DUMPLINGS_FILES'
import json, os, sys
expected = json.load(open(sys.argv[1]))["files"]
root = sys.argv[2]
actual = {{os.path.relpath(os.path.join(d, n), root): os.path.getsize(os.path.join(d, n))
           for d, _, names in os.walk(root) for n in names}}
if actual != expected:
    raise SystemExit("[coinjoin-analysis] the downloaded Scanner/ does not match its manifest")
DUMPLINGS_FILES
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
cp "$RUN_WORK/dumplings-manifest.json" "$OUTPUT/dumplings-snapshot.json"
echo "[coinjoin-analysis] baseline of $(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["coinjoins"]))' "$OUTPUT/coinjoin_tx_info.json") coinjoins through height $SNAPSHOT_HEIGHT"
s5 sync "$OUTPUT/" "$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/"
