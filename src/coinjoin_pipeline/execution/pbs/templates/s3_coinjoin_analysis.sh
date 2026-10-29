{preamble}
{start}
s5 sync "$RUN_PREFIX/*" "$RUN_WORK/"
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
require_file "$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json" \
  "Coinjoin analysis did not produce coinjoin-analysis_data/coinjoin_tx_info.json"
s5 sync "$RUN_WORK/coinjoin-analysis_data/" "$RUN_PREFIX/coinjoin-analysis_data/"
