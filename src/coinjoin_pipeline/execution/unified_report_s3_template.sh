#!/bin/bash
#PBS -N unified_report_s3
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
set -euo pipefail

{stage_header}
IMAGE={image}
mkdir -p \
  "$RUN_WORK/.pbs" \
  "$RUN_WORK/.pipeline/exporters" \
  "$RUN_WORK/coinjoin_emulator_data" \
  "$RUN_WORK/coinjoin-analysis_data" \
  "$RUN_WORK/blocksci-analysis_data" \
  "$RUN_WORK/coinjoin-mappings_data"
stage_finalize() {{
  :
}}
{bootstrap}
{stage_setup}
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
