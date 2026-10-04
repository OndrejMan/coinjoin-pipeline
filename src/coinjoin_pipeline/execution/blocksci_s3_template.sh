#!/bin/bash
#PBS -N blocksci_analysis_s3
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
BITCOIN_DATADIR="$RUN_WORK/bitcoin_data"
if [ ! -d "$BITCOIN_DATADIR/regtest/blocks" ] && [ -d "$BITCOIN_DATADIR/data/regtest/blocks" ]; then
  BITCOIN_DATADIR="$BITCOIN_DATADIR/data"
fi
test -d "$BITCOIN_DATADIR/regtest/blocks" || {{
  echo "BlockSci S3-compatible reporting requires a Bitcoin datadir containing regtest/blocks; checked $RUN_WORK/bitcoin_data and $RUN_WORK/bitcoin_data/data" >&2
  exit 1
}}
test -d "$RUN_WORK/.pipeline/exporters"
{coinjoin_analysis_check}
EXPORTED_MAX_BLOCK="$(find "$RUN_WORK/coinjoin_emulator_data/data/btc-node" -maxdepth 1 -type f -name 'block_*.json' -printf '%f\n' | sed -nE 's/^block_([0-9]+)\.json$/\1/p' | sort -n | tail -n 1)"
test -n "$EXPORTED_MAX_BLOCK"
singularity exec \
  --cleanenv \
  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \
  --bind "$BITCOIN_DATADIR:/mnt/data:ro" \
  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" \
  --env PBS_RUN_ID="$RUN_ID" --env PBS_EXPORTED_MAX_BLOCK="$EXPORTED_MAX_BLOCK" "$IMAGE" \
  bash -c 'cd "/runs/emulation/logs/$PBS_RUN_ID" && EXPORTED_MAX_BLOCK="$PBS_EXPORTED_MAX_BLOCK" && {command}'
{report_output_check}
{analysis_output_check}
{upload_blocksci}
{upload_analysis}
{upload_report}
{upload_logs}
