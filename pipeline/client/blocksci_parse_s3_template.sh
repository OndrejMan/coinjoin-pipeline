#!/bin/bash
#PBS -N blocksci_parse_s3
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
RUN_WORK="$RUNS_ROOT/$RUN_ID"
CACHE_DIR="$RUN_WORK/blocksci-parse_data"
mkdir -p "$RUN_WORK/.pbs" "$RUN_WORK/logs" "$RUN_WORK/coinjoin_emulator_data/data/btc-node" "$CACHE_DIR"
FAILED_MARKER="$RUN_WORK/.pbs/blocksci-parse.failed"
DONE_MARKER="$RUN_WORK/.pbs/blocksci-parse.done"
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
{clear_markers}
export TMPDIR="$SCRATCHDIR" SINGULARITY_CACHEDIR="$SCRATCHDIR" SINGULARITY_TMPDIR="$SCRATCHDIR" SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"
echo "[blocksci-parse] preparing {source_description}"
{prepare_source}
{produce_index}
test -f "$RUN_WORK/blocksci_data/config.json" || {{ echo "BlockSci parser did not produce blocksci_data/config.json" >&2; exit 1; }}
test -f "$RUN_WORK/blocksci_data/parsed/chain/block.dat" || {{ echo "BlockSci parser did not produce parsed/chain/block.dat" >&2; exit 1; }}
echo "[blocksci-parse] archiving reusable parsed index"
tar -C "$RUN_WORK" -czf "$CACHE_DIR/blocksci_data.tar.gz" blocksci_data
(
  cd "$CACHE_DIR"
  sha256sum blocksci_data.tar.gz > blocksci_data.tar.gz.sha256
)
printf '{{\n  "schema_version": "1.0",\n  "run_id": "%s",\n  "blocksci_image": "%s",\n  "source_kind": "%s",\n  "network": "%s",\n  "exported_max_block": %s,\n  "archive": "blocksci_data.tar.gz",\n  "archive_sha256": "%s"\n}}\n' \
  "$RUN_ID" "$IMAGE" "{source_kind}" "{network}" "$EXPORTED_MAX_BLOCK" "$(cut -d ' ' -f 1 "$CACHE_DIR/blocksci_data.tar.gz.sha256")" \
  > "$CACHE_DIR/manifest.json"
{upload_cache}
echo "[blocksci-parse] reusable cache upload complete"
