#!/bin/bash
#PBS -N {job_name}
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
MODE={mode}
test -n "${{SCRATCHDIR:-}}" || {{ echo "SCRATCHDIR is not set" >&2; exit 1; }}
RUNS_ROOT="$SCRATCHDIR/coinjoin-run"
RUN_WORK="$RUNS_ROOT/$RUN_ID"
CACHE_DIR="$RUN_WORK/blocksci-parse_data"
mkdir -p "$RUN_WORK/.pbs" "$RUN_WORK/logs" "$RUN_WORK/.pipeline/exporters" "$CACHE_DIR"
FAILED_MARKER="$RUN_WORK/.pbs/{stage}.failed"
DONE_MARKER="$RUN_WORK/.pbs/{stage}.done"
stage_finalize() {{
  {upload_outputs}
}}
publish_done() {{
  {upload_done}
}}
publish_failed() {{
  {upload_failed}
}}
{bootstrap}
test -r "$S3_CREDENTIALS_FILE" || {{ echo "S3 credentials file is not readable: $S3_CREDENTIALS_FILE" >&2; exit 1; }}
{s5cmd_check}
export TMPDIR="$SCRATCHDIR" SINGULARITY_CACHEDIR="$SCRATCHDIR" SINGULARITY_TMPDIR="$SCRATCHDIR" SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"
echo "[$MODE] downloading required reusable BlockSci inputs"
{download_inputs}
{restore_cache}
{prepare_mode}
EXTRA_BINDS=()
{extra_binds}
echo "[$MODE] starting on $(hostname -f)"
{connection_help}
singularity exec \
  --cleanenv \
  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \
  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" \
  "${{EXTRA_BINDS[@]}}" \
  --env PBS_RUN_ID="$RUN_ID" \
  --env ACTIVE_RUN_ID="$RUN_ID" \
  --env BLOCKSCI_CONFIG="/runs/emulation/logs/$RUN_ID/blocksci_data/config.json" \
  --env BLOCKSCI_RUN_DIR="/runs/emulation/logs/$RUN_ID" \
  "$IMAGE" \
  bash -c 'cd "/runs/emulation/logs/$PBS_RUN_ID" && {command}'
{output_check}
echo "[$MODE] completed"
