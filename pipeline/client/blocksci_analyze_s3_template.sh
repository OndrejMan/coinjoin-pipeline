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
on_exit() {{
  status=$?
  trap - EXIT TERM
  set +e
  upload_status=0
  {upload_outputs}
  if [ "$status" -eq 0 ] && [ "$upload_status" -ne 0 ]; then
    status=$upload_status
  fi
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
echo "[$MODE] downloading required reusable BlockSci inputs"
{download_inputs}
test -f "$CACHE_DIR/blocksci_data.tar.gz" || {{ echo "Reusable BlockSci cache is missing blocksci-parse_data/blocksci_data.tar.gz" >&2; exit 1; }}
test -f "$CACHE_DIR/blocksci_data.tar.gz.sha256" || {{ echo "Reusable BlockSci cache is missing its SHA-256 sidecar" >&2; exit 1; }}
test -f "$CACHE_DIR/manifest.json" || {{ echo "Reusable BlockSci cache is missing blocksci-parse_data/manifest.json" >&2; exit 1; }}
grep -Fq "\"schema_version\": \"1.0\"" "$CACHE_DIR/manifest.json" || {{ echo "Unsupported reusable BlockSci cache manifest schema" >&2; exit 1; }}
grep -Fq "\"run_id\": \"$RUN_ID\"" "$CACHE_DIR/manifest.json" || {{ echo "Reusable BlockSci cache run ID does not match $RUN_ID" >&2; exit 1; }}
grep -Fq "\"blocksci_image\": \"$IMAGE\"" "$CACHE_DIR/manifest.json" || {{ echo "Reusable BlockSci cache was produced by a different BlockSci image" >&2; exit 1; }}
SIDECAR_SHA="$(cut -d ' ' -f 1 "$CACHE_DIR/blocksci_data.tar.gz.sha256")"
grep -Fq "\"archive_sha256\": \"$SIDECAR_SHA\"" "$CACHE_DIR/manifest.json" || {{ echo "Reusable BlockSci cache manifest SHA-256 does not match its sidecar" >&2; exit 1; }}
(
  cd "$CACHE_DIR"
  sha256sum -c blocksci_data.tar.gz.sha256
)
tar -C "$RUN_WORK" -xzf "$CACHE_DIR/blocksci_data.tar.gz"
test -f "$RUN_WORK/blocksci_data/config.json" || {{ echo "Reusable BlockSci cache did not contain blocksci_data/config.json" >&2; exit 1; }}
test -f "$RUN_WORK/blocksci_data/parsed/chain/block.dat" || {{ echo "Reusable BlockSci cache did not contain parsed/chain/block.dat" >&2; exit 1; }}
{prepare_mode}
EXTRA_BINDS=()
{extra_binds}
echo "[$MODE] starting on $(hostname -f)"
{connection_help}
singularity exec \
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
