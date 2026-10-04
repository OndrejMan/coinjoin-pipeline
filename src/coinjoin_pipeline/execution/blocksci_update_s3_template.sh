#!/bin/bash
#PBS -N blocksci_update_s3
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
set -euo pipefail

ARTIFACT_URI={artifact_uri}
RUN_ID={run_id}
SOURCE_RUN_ID={source_run_id}
S3_ENDPOINT_URL={endpoint_url}
S3_CREDENTIALS_FILE={credentials_file}
S3_PROFILE={profile}
IMAGE={image}
NETWORK={network}
EXPORTED_MAX_BLOCK={exported_max_block}
test -n "${{SCRATCHDIR:-}}" || {{ echo "SCRATCHDIR is not set" >&2; exit 1; }}
RUNS_ROOT="$SCRATCHDIR/coinjoin-run"
RUN_WORK="$RUNS_ROOT/$RUN_ID"
SOURCE_CACHE_DIR="$RUN_WORK/source-blocksci-parse_data"
CACHE_DIR="$RUN_WORK/blocksci-parse_data"
FAILED_MARKER="$RUN_WORK/.pbs/blocksci-update.failed"
DONE_MARKER="$RUN_WORK/.pbs/blocksci-update.done"
mkdir -p "$RUN_WORK/.pbs" "$SOURCE_CACHE_DIR" "$CACHE_DIR"
stage_finalize() {{
  :
}}
publish_done() {{
  {upload_done}
}}
publish_failed() {{
  {upload_failed}
}}
{bootstrap}
test -r "$S3_CREDENTIALS_FILE" || {{ echo "S3 credentials file is not readable: $S3_CREDENTIALS_FILE" >&2; exit 1; }}
{prepare_source}
{s5cmd_check}
mkdir -p "$RUN_WORK/.pipeline/exporters"
{download_exporters}
export TMPDIR="$SCRATCHDIR" SINGULARITY_CACHEDIR="$SCRATCHDIR" SINGULARITY_TMPDIR="$SCRATCHDIR" SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"

echo "[blocksci-update] restoring verified cache from run $SOURCE_RUN_ID"
{download_source_cache}
{restore_cache}
SOURCE_MAX_BLOCK="$(sed -nE 's/.*"exported_max_block"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' "$SOURCE_CACHE_DIR/manifest.json" | head -n 1)"
test -n "$SOURCE_MAX_BLOCK" || {{ echo "Source cache manifest has no exported_max_block" >&2; exit 1; }}
[ "$EXPORTED_MAX_BLOCK" -gt "$SOURCE_MAX_BLOCK" ] || {{ echo "Target maximum block $EXPORTED_MAX_BLOCK must be greater than source maximum block $SOURCE_MAX_BLOCK" >&2; exit 1; }}
MANIFEST_EXTRA=""
{prepare_blocks}

{canonicalize_index}
MAX_BLOCK_NUM="$((EXPORTED_MAX_BLOCK + 1))"
sed -i -E 's#("maxBlockNum"[[:space:]]*:[[:space:]]*)-?[0-9]+#\1'"$MAX_BLOCK_NUM"'#' "$RUN_WORK/blocksci_data/config.json"
grep -Eq '"maxBlockNum"[[:space:]]*:[[:space:]]*'"$MAX_BLOCK_NUM"'([,[:space:]]|$)' "$RUN_WORK/blocksci_data/config.json" || {{ echo "Could not update parser.maxBlockNum" >&2; exit 1; }}

{parse_chain}

{verify_index}
MANIFEST_EXTRA="$MANIFEST_EXTRA,
  \"cache_operation\": \"incremental-update\",
  \"source_run_id\": \"$SOURCE_RUN_ID\",
  \"source_exported_max_block\": $SOURCE_MAX_BLOCK"
{publish_cache}
echo "[blocksci-update] updated cache upload complete: $SOURCE_RUN_ID -> $RUN_ID"
