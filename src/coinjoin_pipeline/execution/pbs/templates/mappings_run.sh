# Run the enumerator and Sake over $INPUT into $OUT; both read and write under $WORK.
ENUMERATOR_SIF="$SCRATCHDIR/coinjoin-mappings-enumerator.sif"
SAKE_SIF="$SCRATCHDIR/coinjoin-mappings-sake.sif"
fetch_sif() {{
  case "$1" in
    docker://*|oras://*|library://*|http://*|https://*) singularity pull --force "$2" "$1" ;;
    *) test -f "$1"; cp "$1" "$2" ;;
  esac
}}
fetch_sif "$ENUMERATOR_IMAGE" "$ENUMERATOR_SIF"
fetch_sif "$SAKE_IMAGE" "$SAKE_SIF"
# A pinned reference names its registry digest; a pull of a bare tag has none, because the
# SIF built from it hashes differently on every pull. A local SIF file is its own identity.
image_digest() {{
  case "$1" in
    *@sha256:*) echo "sha256:${{1##*@sha256:}}" ;;
    docker://*|oras://*|library://*|http://*|https://*) ;;
    *) echo "sha256:$(sha256sum "$2" | awk '{{print $1}}')" ;;
  esac
}}
ENUMERATOR_DIGEST="$(image_digest "$ENUMERATOR_IMAGE" "$ENUMERATOR_SIF")"
SAKE_DIGEST="$(image_digest "$SAKE_IMAGE" "$SAKE_SIF")"
in_enumerator() {{
  singularity exec --bind "$WORK:$WORK:rw" "$ENUMERATOR_SIF" "$@"
}}
# Exit status 1 is a partial result only when the tool's summary counts errors.
accept_partial() {{
  if [ "$1" -ne 0 ]; then
    test "$1" -eq 1
    in_enumerator python3 -c \
      'import json,sys; d=json.load(open(sys.argv[1])); assert d.get("summary",{{}}).get("errors",0)>0' "$2"
  fi
}}

set +e
in_enumerator python3 /app/run.py \
  "$INPUT" --output "$OUT/enumerator.json" \
  --mining-fee-rate {mining_fee_rate} --coordination-fee-rate {coordination_fee_rate} \
  --max-decomposition-fee {max_decomposition_fee} --mode {mode} \
  --timeout {timeout} --retry-timeout {retry_timeout}
ENUMERATOR_STATUS=$?
set -e
accept_partial "$ENUMERATOR_STATUS" "$OUT/enumerator.json"

set +e
singularity exec --bind "$WORK:$WORK:rw" "$SAKE_SIF" dotnet /app/Sake.dll \
  --input "$INPUT" --output "$OUT/sake.json" --seed {sake_seed}
SAKE_STATUS=$?
set -e
accept_partial "$SAKE_STATUS" "$OUT/sake.json"

# The only place that decides the stage's status: a timeout or an error in either summary
# makes it partial. The report shows the status as written here.
in_enumerator python3 - "$OUT" "$ENUMERATOR_IMAGE" "$SAKE_IMAGE" "$ENUMERATOR_DIGEST" "$SAKE_DIGEST" <<'PY'
import json
import sys

out, enumerator_image, sake_image, enumerator_digest, sake_digest = sys.argv[1:]
with open(out + "/enumerator.json") as stream:
    enumerator = json.load(stream)
with open(out + "/sake.json") as stream:
    sake = json.load(stream)
enumerator_summary, sake_summary = enumerator.get("summary", {{}}), sake.get("summary", {{}})
failures = (enumerator_summary.get("timed_out", 0), enumerator_summary.get("errors", 0), sake_summary.get("errors", 0))
combined = {{
    "schema_version": "1.0",
    "status": "partial" if any(failures) else "complete",
    "provenance": {{
        "enumerator_image": enumerator_image,
        "sake_image": sake_image,
        "enumerator_image_digest": enumerator_digest or None,
        "sake_image_digest": sake_digest or None,
    }},
    "enumerator": enumerator,
    "sake": sake,
}}
with open(out + "/coinjoin_mappings.json", "w") as stream:
    json.dump(combined, stream, indent=2, sort_keys=True)
PY
test -f "$OUT/coinjoin_mappings.json" || {{ echo "CoinJoin mappings did not produce coinjoin_mappings.json" >&2; exit 1; }}
