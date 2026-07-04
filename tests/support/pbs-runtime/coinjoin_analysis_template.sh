#!/bin/bash
#PBS -N coinjoin_analysis
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe

set -euo pipefail

cd "$PBS_O_WORKDIR"

test -n "$SCRATCHDIR" || {{ echo "SCRATCHDIR is not set"; exit 1; }}

export SINGULARITY_CACHEDIR="$SCRATCHDIR"
export SINGULARITY_TMPDIR="$SCRATCHDIR"
export SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"

RUN_DIR="{run_dir}"
OUTPUT_DIR="{output_dir}"
INPUT_DATA_DIR="{input_data_dir}"
IMAGE="{image}"

mkdir -p "$OUTPUT_DIR"
mkdir -p "$RUN_DIR/logs"
mkdir -p "$RUN_DIR/.pbs"
CONTAINER_WORK_ROOT="$SCRATCHDIR/coinjoin-analysis-selected"
mkdir -p "$CONTAINER_WORK_ROOT/$(basename "$RUN_DIR")"
rm -f "$RUN_DIR/.pbs/coinjoin-analysis.done" "$RUN_DIR/.pbs/coinjoin-analysis.failed"

trap 'echo failed > "$RUN_DIR/.pbs/coinjoin-analysis.failed"' ERR

cat > "$RUN_DIR/.pbs/coinjoin-analysis-stage.sh" <<'PBS_COINJOIN_ANALYSIS_STAGE'
#!/bin/bash
set -euo pipefail
cd /runs/emulation/selected/"${{PBS_RUN_ID}}"
{coinjoin_analysis_command}
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

echo done > "$RUN_DIR/.pbs/coinjoin-analysis.done"
