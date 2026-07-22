#!/bin/bash
#PBS -N coinjoin_mappings_s3
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
set -euo pipefail

ARTIFACT_URI={artifact_uri}
RUN_ID={run_id}
S3_ENDPOINT_URL={endpoint_url}
S3_CREDENTIALS_FILE={credentials_file}
S3_PROFILE={profile}
ENUMERATOR_IMAGE={enumerator_image}
SAKE_IMAGE={sake_image}
test -n "${{SCRATCHDIR:-}}" || {{ echo "SCRATCHDIR is not set" >&2; exit 1; }}
RUN_WORK="$SCRATCHDIR/coinjoin-run/$RUN_ID"
OUT="$RUN_WORK/coinjoin-mappings_data"
FAILED_MARKER="$RUN_WORK/.pbs/coinjoin-mappings.failed"
DONE_MARKER="$RUN_WORK/.pbs/coinjoin-mappings.done"
mkdir -p "$RUN_WORK/.pbs" "$RUN_WORK/coinjoin-analysis_data" "$OUT"
on_exit() {{
  status=$?
  trap - EXIT TERM
  set +e
  upload_status=0
  if [ -d "$OUT" ]; then
    {upload_outputs} || upload_status=$?
  fi
  if [ "$status" -eq 0 ] && [ "$upload_status" -ne 0 ]; then
    status=$upload_status
  fi
  if [ "$status" -eq 0 ]; then
    printf 'done\n' > "$DONE_MARKER"
    {upload_done}
  else
    printf 'failed\n' > "$FAILED_MARKER"
    {upload_failed}
  fi
  exit "$status"
}}
trap on_exit EXIT
trap 'exit 143' TERM
test -r "$S3_CREDENTIALS_FILE" || {{ echo "S3 credentials file is not readable: $S3_CREDENTIALS_FILE" >&2; exit 1; }}
{s5cmd_check}
{clear_markers}
export TMPDIR="$SCRATCHDIR" SINGULARITY_CACHEDIR="$SCRATCHDIR" SINGULARITY_TMPDIR="$SCRATCHDIR" SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"

echo "[coinjoin-mappings] downloading coinjoin-analysis input"
{download_input}
INPUT="$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json"
test -f "$INPUT" || {{ echo "CoinJoin mappings require coinjoin-analysis_data/coinjoin_tx_info.json" >&2; exit 1; }}

ENUMERATOR_SIF="$SCRATCHDIR/coinjoin-mappings-enumerator.sif"
SAKE_SIF="$SCRATCHDIR/coinjoin-mappings-sake.sif"
case "$ENUMERATOR_IMAGE" in
  docker://*|oras://*|library://*|http://*|https://*) singularity pull --force "$ENUMERATOR_SIF" "$ENUMERATOR_IMAGE" ;;
  *) test -f "$ENUMERATOR_IMAGE"; cp "$ENUMERATOR_IMAGE" "$ENUMERATOR_SIF" ;;
esac
case "$SAKE_IMAGE" in
  docker://*|oras://*|library://*|http://*|https://*) singularity pull --force "$SAKE_SIF" "$SAKE_IMAGE" ;;
  *) test -f "$SAKE_IMAGE"; cp "$SAKE_IMAGE" "$SAKE_SIF" ;;
esac
ENUMERATOR_DIGEST="sha256:$(sha256sum "$ENUMERATOR_SIF" | awk '{{print $1}}')"
SAKE_DIGEST="sha256:$(sha256sum "$SAKE_SIF" | awk '{{print $1}}')"

set +e
singularity exec --bind "$RUN_WORK:$RUN_WORK:rw" "$ENUMERATOR_SIF" python3 /app/run.py \
  "$INPUT" --output "$OUT/enumerator.json" \
  --mining-fee-rate {mining_fee_rate} --coordination-fee-rate {coordination_fee_rate} \
  --max-decomposition-fee {max_decomposition_fee} --mode {mode} \
  --timeout {timeout} --retry-timeout {retry_timeout}
ENUMERATOR_STATUS=$?
set -e
if [ "$ENUMERATOR_STATUS" -ne 0 ]; then
  test "$ENUMERATOR_STATUS" -eq 1
  singularity exec --bind "$RUN_WORK:$RUN_WORK:rw" "$ENUMERATOR_SIF" python3 -c \
    'import json,sys; d=json.load(open(sys.argv[1])); assert d.get("summary",{{}}).get("errors",0)>0' \
    "$OUT/enumerator.json"
fi

set +e
singularity exec --bind "$RUN_WORK:$RUN_WORK:rw" "$SAKE_SIF" dotnet /app/Sake.dll \
  --input "$INPUT" --output "$OUT/sake.json" --seed {sake_seed}
SAKE_STATUS=$?
set -e
if [ "$SAKE_STATUS" -ne 0 ]; then
  test "$SAKE_STATUS" -eq 1
  singularity exec --bind "$RUN_WORK:$RUN_WORK:rw" "$ENUMERATOR_SIF" python3 -c \
    'import json,sys; d=json.load(open(sys.argv[1])); assert d.get("summary",{{}}).get("errors",0)>0' \
    "$OUT/sake.json"
fi

singularity exec --bind "$RUN_WORK:$RUN_WORK:rw" "$ENUMERATOR_SIF" python3 -c \
  'import json,sys; p,ed,sd=sys.argv[1:]; e=json.load(open(p+"/enumerator.json")); s=json.load(open(p+"/sake.json")); es=e.get("summary",{{}}); ss=s.get("summary",{{}}); status="partial" if any((es.get("timed_out",0),es.get("errors",0),ss.get("errors",0))) else "complete"; json.dump({{"schema_version":"1.0","status":status,"provenance":{{"enumerator_image":"{enumerator_image_value}","sake_image":"{sake_image_value}","enumerator_image_digest":ed,"sake_image_digest":sd}},"enumerator":e,"sake":s}},open(p+"/coinjoin_mappings.json","w"),indent=2,sort_keys=True)' \
  "$OUT" "$ENUMERATOR_DIGEST" "$SAKE_DIGEST"
test -f "$OUT/coinjoin_mappings.json" || {{ echo "CoinJoin mappings did not produce coinjoin_mappings.json" >&2; exit 1; }}
echo "[coinjoin-mappings] completed"
