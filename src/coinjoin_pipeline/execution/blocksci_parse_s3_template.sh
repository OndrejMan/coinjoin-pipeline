#!/bin/bash
#PBS -N blocksci_parse_s3
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
set -euo pipefail

{stage_header}
IMAGE={image}
CACHE_DIR="$RUN_WORK/blocksci-parse_data"
mkdir -p "$RUN_WORK/.pbs" "$RUN_WORK/logs" "$RUN_WORK/coinjoin_emulator_data/data/btc-node" "$CACHE_DIR"
JOB_LOG="$RUN_WORK/logs/blocksci-parse.pbs.log"
stage_finalize() {{
  exec 1>&3 2>&4
  exec 3>&- 4>&-
  {upload_log} || upload_status=$?
}}
{bootstrap}
exec 3>&1 4>&2
exec >"$JOB_LOG" 2>&1
{stage_setup}
mkdir -p "$RUN_WORK/.pipeline/exporters"
{download_exporters}
echo "[blocksci-parse] preparing {source_description}"
MANIFEST_EXTRA=""
{prepare_source}
{produce_index}
test -f "$RUN_WORK/blocksci_data/config.json" || {{ echo "BlockSci parser did not produce blocksci_data/config.json" >&2; exit 1; }}
test -f "$RUN_WORK/blocksci_data/parsed/chain/block.dat" || {{ echo "BlockSci parser did not produce parsed/chain/block.dat" >&2; exit 1; }}
{verify_index}
{publish_cache}
echo "[blocksci-parse] reusable cache upload complete"
