#!/bin/bash
#PBS -N coinjoin_analysis_s3
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
set -euo pipefail

ARTIFACT_URI={artifact_uri}
RUN_ID={run_id}
S3_ENDPOINT_URL={endpoint_url}
S3_CREDENTIALS_FILE={credentials_file}
S3_PROFILE={profile}
IMAGE={image}
test -n "${{SCRATCHDIR:-}}" || {{ echo "SCRATCHDIR is not set" >&2; exit 1; }}
RUN_WORK="$SCRATCHDIR/coinjoin-run/$RUN_ID"
mkdir -p "$RUN_WORK/.pbs" "$RUN_WORK/logs"
FAILED_MARKER="$RUN_WORK/.pbs/coinjoin-analysis.failed"
DONE_MARKER="$RUN_WORK/.pbs/coinjoin-analysis.done"
on_exit() {{
  status=$?
  trap - EXIT TERM
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
export TMPDIR="$SCRATCHDIR" SINGULARITY_CACHEDIR="$SCRATCHDIR" SINGULARITY_TMPDIR="$SCRATCHDIR" SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"
{download_run}
mkdir -p "$RUN_WORK/coinjoin-analysis_data"
singularity exec \
  --bind "$RUN_WORK:/runs/emulation/selected/$RUN_ID:rw" \
  --env PBS_RUN_ID="$RUN_ID" "$IMAGE" \
  bash -c 'cd "/runs/emulation/selected/$PBS_RUN_ID" && {command}'
{upload_results}
