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
ENUMERATOR_DIGEST="sha256:$(sha256sum "$ENUMERATOR_SIF" | awk '{{print $1}}')"
SAKE_DIGEST="sha256:$(sha256sum "$SAKE_SIF" | awk '{{print $1}}')"
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

in_enumerator python3 -c \
  'import json,sys; p,ei,si,ed,sd=sys.argv[1:]; e=json.load(open(p+"/enumerator.json")); s=json.load(open(p+"/sake.json")); es=e.get("summary",{{}}); ss=s.get("summary",{{}}); status="partial" if any((es.get("timed_out",0),es.get("errors",0),ss.get("errors",0))) else "complete"; json.dump({{"schema_version":"1.0","status":status,"provenance":{{"enumerator_image":ei,"sake_image":si,"enumerator_image_digest":ed,"sake_image_digest":sd}},"enumerator":e,"sake":s}},open(p+"/coinjoin_mappings.json","w"),indent=2,sort_keys=True)' \
  "$OUT" "$ENUMERATOR_IMAGE" "$SAKE_IMAGE" "$ENUMERATOR_DIGEST" "$SAKE_DIGEST"
test -f "$OUT/coinjoin_mappings.json" || {{ echo "CoinJoin mappings did not produce coinjoin_mappings.json" >&2; exit 1; }}
