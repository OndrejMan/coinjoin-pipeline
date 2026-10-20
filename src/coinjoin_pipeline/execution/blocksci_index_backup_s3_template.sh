#!/bin/bash
#PBS -N blocksci_index_backup_s3
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_{scratch_type}={scratch}{multijob}
#PBS -l walltime={walltime}
#PBS -j oe
set -euo pipefail

{stage_header}
mkdir -p "$RUN_WORK/.pbs" "$RUN_WORK/logs"
stage_finalize() {{
  {finalize_index}
}}
{bootstrap}
{stage_setup}
{index_reader}
INDEX_ZSTD_THREADS={ncpus}
index_backup
index_unlock
{record_backup}
echo "[blocksci-backup] index at height $(index_height "$INDEX_DIR") backed up to $INDEX_BACKUP"
