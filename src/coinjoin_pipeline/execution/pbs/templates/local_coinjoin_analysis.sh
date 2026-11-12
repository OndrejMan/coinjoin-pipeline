{preamble}
IMAGE="{image}"
OUTPUT_DIR="{output_dir}"
INPUT_DATA_DIR="{input_data_dir}"
mkdir -p "$OUTPUT_DIR"
CONTAINER_WORK_ROOT="$SCRATCHDIR/coinjoin-analysis-selected"
mkdir -p "$CONTAINER_WORK_ROOT/$(basename "$RUN_DIR")"

cat > "$RUN_DIR/.pbs/coinjoin-analysis-stage.sh" <<'PBS_COINJOIN_ANALYSIS_STAGE'
#!/bin/bash
set -euo pipefail
cd /runs/emulation/selected/"${{PBS_RUN_ID}}"
{command}
PBS_COINJOIN_ANALYSIS_STAGE
chmod 700 "$RUN_DIR/.pbs/coinjoin-analysis-stage.sh"

singularity exec \
  --bind /storage:/storage \
  --bind "$CONTAINER_WORK_ROOT:/runs/emulation/selected:rw" \
  --bind "$OUTPUT_DIR:/runs/emulation/selected/$(basename "$RUN_DIR"):rw" \
  --bind "$INPUT_DATA_DIR:/runs/emulation/selected/$(basename "$RUN_DIR")/data:ro" \
  --bind "$RUN_DIR/.pbs:/runs/emulation/selected/$(basename "$RUN_DIR")/.pbs:rw" \
  --env PBS_RUN_ID="$(basename "$RUN_DIR")" \
  "$IMAGE" \
  bash "/runs/emulation/selected/$(basename "$RUN_DIR")/.pbs/coinjoin-analysis-stage.sh"
