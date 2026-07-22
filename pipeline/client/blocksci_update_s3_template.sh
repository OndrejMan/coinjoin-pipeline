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
BITCOIN_DATADIR={bitcoin_datadir}
test -n "${{SCRATCHDIR:-}}" || {{ echo "SCRATCHDIR is not set" >&2; exit 1; }}
RUNS_ROOT="$SCRATCHDIR/coinjoin-run"
RUN_WORK="$RUNS_ROOT/$RUN_ID"
SOURCE_CACHE_DIR="$RUN_WORK/source-blocksci-parse_data"
CACHE_DIR="$RUN_WORK/blocksci-parse_data"
FAILED_MARKER="$RUN_WORK/.pbs/blocksci-update.failed"
DONE_MARKER="$RUN_WORK/.pbs/blocksci-update.done"
mkdir -p "$RUN_WORK/.pbs" "$SOURCE_CACHE_DIR" "$CACHE_DIR"
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
test -d "$BITCOIN_DATADIR/blocks" || {{ echo "External Bitcoin coin directory must contain blocks/: $BITCOIN_DATADIR" >&2; exit 1; }}
{s5cmd_check}
{clear_markers}
export TMPDIR="$SCRATCHDIR" SINGULARITY_CACHEDIR="$SCRATCHDIR" SINGULARITY_TMPDIR="$SCRATCHDIR" SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"

echo "[blocksci-update] downloading cache from run $SOURCE_RUN_ID"
{download_source_cache}
test -f "$SOURCE_CACHE_DIR/blocksci_data.tar.gz" || {{ echo "Source cache is missing blocksci_data.tar.gz" >&2; exit 1; }}
test -f "$SOURCE_CACHE_DIR/blocksci_data.tar.gz.sha256" || {{ echo "Source cache is missing its SHA-256 sidecar" >&2; exit 1; }}
test -f "$SOURCE_CACHE_DIR/manifest.json" || {{ echo "Source cache is missing manifest.json" >&2; exit 1; }}
grep -Fq '"schema_version": "1.0"' "$SOURCE_CACHE_DIR/manifest.json" || {{ echo "Unsupported source cache manifest schema" >&2; exit 1; }}
grep -Fq "\"run_id\": \"$SOURCE_RUN_ID\"" "$SOURCE_CACHE_DIR/manifest.json" || {{ echo "Source cache run ID does not match $SOURCE_RUN_ID" >&2; exit 1; }}
grep -Fq "\"blocksci_image\": \"$IMAGE\"" "$SOURCE_CACHE_DIR/manifest.json" || {{ echo "Source cache was produced by a different BlockSci image" >&2; exit 1; }}
grep -Fq '"source_kind": "external-bitcoin"' "$SOURCE_CACHE_DIR/manifest.json" || {{ echo "Incremental update requires an external-bitcoin cache" >&2; exit 1; }}
grep -Fq "\"network\": \"$NETWORK\"" "$SOURCE_CACHE_DIR/manifest.json" || {{ echo "Source cache network does not match $NETWORK" >&2; exit 1; }}
SIDECAR_SHA="$(cut -d ' ' -f 1 "$SOURCE_CACHE_DIR/blocksci_data.tar.gz.sha256")"
grep -Fq "\"archive_sha256\": \"$SIDECAR_SHA\"" "$SOURCE_CACHE_DIR/manifest.json" || {{ echo "Source cache manifest SHA-256 does not match its sidecar" >&2; exit 1; }}
(
  cd "$SOURCE_CACHE_DIR"
  sha256sum -c blocksci_data.tar.gz.sha256
)
SOURCE_MAX_BLOCK="$(sed -nE 's/.*"exported_max_block"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' "$SOURCE_CACHE_DIR/manifest.json" | head -n 1)"
test -n "$SOURCE_MAX_BLOCK" || {{ echo "Source cache manifest has no exported_max_block" >&2; exit 1; }}
[ "$EXPORTED_MAX_BLOCK" -gt "$SOURCE_MAX_BLOCK" ] || {{ echo "Target maximum block $EXPORTED_MAX_BLOCK must be greater than source maximum block $SOURCE_MAX_BLOCK" >&2; exit 1; }}

echo "[blocksci-update] restoring verified index through block $SOURCE_MAX_BLOCK"
tar -C "$RUN_WORK" -xzf "$SOURCE_CACHE_DIR/blocksci_data.tar.gz"
CONFIG="$RUN_WORK/blocksci_data/config.json"
test -f "$CONFIG" || {{ echo "Source cache did not contain blocksci_data/config.json" >&2; exit 1; }}
test -f "$RUN_WORK/blocksci_data/parsed/chain/block.dat" || {{ echo "Source cache did not contain parsed/chain/block.dat" >&2; exit 1; }}
CANONICAL_PARSED="/runs/emulation/logs/$RUN_ID/blocksci_data/parsed"
sed -i -E 's#("dataDirectory"[[:space:]]*:[[:space:]]*)"[^"]*"#\1"'"$CANONICAL_PARSED"'"#' "$CONFIG"
grep -Fq "$CANONICAL_PARSED" "$CONFIG" || {{ echo "Could not canonicalize BlockSci dataDirectory" >&2; exit 1; }}
MAX_BLOCK_NUM="$((EXPORTED_MAX_BLOCK + 1))"
sed -i -E 's#("maxBlockNum"[[:space:]]*:[[:space:]]*)-?[0-9]+#\1'"$MAX_BLOCK_NUM"'#' "$CONFIG"
grep -Eq '"maxBlockNum"[[:space:]]*:[[:space:]]*'"$MAX_BLOCK_NUM"'([,[:space:]]|$)' "$CONFIG" || {{ echo "Could not update parser.maxBlockNum" >&2; exit 1; }}

echo "[blocksci-update] parsing blocks $((SOURCE_MAX_BLOCK + 1)) through $EXPORTED_MAX_BLOCK"
singularity exec \
  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \
  --bind "$BITCOIN_DATADIR:/mnt/data:ro" \
  --env PBS_RUN_ID="$RUN_ID" "$IMAGE" \
  bash -c 'cd "/runs/emulation/logs/$PBS_RUN_ID" && {command}'

echo "[blocksci-update] archiving updated reusable index"
tar -C "$RUN_WORK" -czf "$CACHE_DIR/blocksci_data.tar.gz" blocksci_data
(
  cd "$CACHE_DIR"
  sha256sum blocksci_data.tar.gz > blocksci_data.tar.gz.sha256
)
printf '{{\n  "schema_version": "1.0",\n  "run_id": "%s",\n  "blocksci_image": "%s",\n  "source_kind": "external-bitcoin",\n  "network": "%s",\n  "exported_max_block": %s,\n  "cache_operation": "incremental-update",\n  "source_run_id": "%s",\n  "source_exported_max_block": %s,\n  "archive": "blocksci_data.tar.gz",\n  "archive_sha256": "%s"\n}}\n' \
  "$RUN_ID" "$IMAGE" "$NETWORK" "$EXPORTED_MAX_BLOCK" "$SOURCE_RUN_ID" "$SOURCE_MAX_BLOCK" "$(cut -d ' ' -f 1 "$CACHE_DIR/blocksci_data.tar.gz.sha256")" \
  > "$CACHE_DIR/manifest.json"
{upload_cache}
echo "[blocksci-update] updated cache upload complete: $SOURCE_RUN_ID -> $RUN_ID"
