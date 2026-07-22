#!/bin/bash
#PBS -N unified_report_s3
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
RUNS_ROOT="$SCRATCHDIR/coinjoin-run"
RUN_WORK="$SCRATCHDIR/coinjoin-run/$RUN_ID"
mkdir -p \
  "$RUN_WORK/.pbs" \
  "$RUN_WORK/.pipeline/exporters" \
  "$RUN_WORK/coinjoin_emulator_data" \
  "$RUN_WORK/coinjoin-analysis_data" \
  "$RUN_WORK/blocksci-analysis_data" \
  "$RUN_WORK/coinjoin-mappings_data"
FAILED_MARKER="$RUN_WORK/.pbs/unified-report.failed"
DONE_MARKER="$RUN_WORK/.pbs/unified-report.done"
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
echo "[unified-report] downloading lightweight report inputs"
{download_inputs}
test -f "$RUN_WORK/blocksci-analysis_data/blocksci_analysis.json" || {{
  echo "Unified S3 report requires blocksci-analysis_data/blocksci_analysis.json" >&2
  exit 1
}}
test -f "$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json" || {{
  echo "Unified S3 report requires coinjoin-analysis_data/coinjoin_tx_info.json" >&2
  exit 1
}}
test -f "$RUN_WORK/.pipeline/exporters/unified_report.py" || {{
  echo "Unified S3 report requires .pipeline/exporters/unified_report.py" >&2
  exit 1
}}
echo "[unified-report] assembling JSON and Markdown from precomputed analyzer outputs"
singularity exec \
  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \
  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" \
  --env PBS_RUN_ID="$RUN_ID" "$IMAGE" \
  bash -c 'cd "/runs/emulation/logs/$PBS_RUN_ID" && {command}'
REPORT_DIR="$RUN_WORK/coinjoinPipeline_data"
test -f "$REPORT_DIR/unified_report.json" || {{
  echo "Unified S3 report did not produce coinjoinPipeline_data/unified_report.json" >&2
  exit 1
}}
{upload_report}
echo "[unified-report] report upload complete"
