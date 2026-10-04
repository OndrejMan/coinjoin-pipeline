#!/bin/bash
#PBS -N coinjoin_analysis_s3
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
set -euo pipefail

{stage_header}
IMAGE={image}
mkdir -p "$RUN_WORK/.pbs" "$RUN_WORK/logs"
stage_finalize() {{
  :
}}
{bootstrap}
{stage_setup}
{download_run}
test -d "$RUN_WORK/coinjoin_emulator_data/data" || {{
  echo "Coinjoin analysis S3-compatible reporting requires coinjoin_emulator_data/data" >&2
  exit 1
}}
mkdir -p "$RUN_WORK/coinjoin-analysis_data"
CONTAINER_WORK_ROOT="$SCRATCHDIR/coinjoin-analysis-selected"
mkdir -p "$CONTAINER_WORK_ROOT/$RUN_ID"
singularity exec \
  --bind "$CONTAINER_WORK_ROOT:/runs/emulation/selected:rw" \
  --bind "$RUN_WORK/coinjoin-analysis_data:/runs/emulation/selected/$RUN_ID:rw" \
  --bind "$RUN_WORK/coinjoin_emulator_data/data:/runs/emulation/selected/$RUN_ID/data:ro" \
  --env PBS_RUN_ID="$RUN_ID" "$IMAGE" \
  bash -c 'cd "/runs/emulation/selected/$PBS_RUN_ID" && {command}'
test -f "$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json" || {{
  echo "Coinjoin analysis did not produce coinjoin-analysis_data/coinjoin_tx_info.json" >&2
  exit 1
}}
{upload_results}
