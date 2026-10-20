{preamble}
CACHE_DIR="$RUN_WORK/blocksci-parse_data"
OUTPUT_DIR={output_dir}
mkdir -p "$RUN_WORK/.pipeline/exporters" "$CACHE_DIR"
stage_finalize() {{
  {upload_outputs}
  {finalize_index}
}}
{start}
require_zstd
echo "[$STAGE] downloading required reusable BlockSci inputs"
{download_inputs}
{restore_cache}
{prepare_mode}
EXTRA_BINDS=()
{extra_binds}
echo "[$STAGE] starting on $(hostname -f)"
{connection_help}
singularity exec \
  --cleanenv \
  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \
  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" \
  "${{EXTRA_BINDS[@]}}" \
  "${{INDEX_BINDS[@]}}" \
  --env PBS_RUN_ID="$RUN_ID" \
  --env ACTIVE_RUN_ID="$RUN_ID" \
  --env BLOCKSCI_CONFIG="/runs/emulation/logs/$RUN_ID/blocksci_data/config.json" \
  --env BLOCKSCI_RUN_DIR="/runs/emulation/logs/$RUN_ID" \
  "$IMAGE" \
  bash -c 'cd "/runs/emulation/logs/$PBS_RUN_ID" && {command}'
{output_check}
echo "[$STAGE] completed"
