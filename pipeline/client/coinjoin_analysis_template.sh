#!/bin/bash
#PBS -N coinjoin_analysis
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
#PBS -o {run_dir}/logs/coinjoin-analysis.pbs.log

set -euo pipefail

cd "$PBS_O_WORKDIR"

RUN_DIR="{run_dir}"
OUTPUT_DIR="{output_dir}"
INPUT_DATA_DIR="{input_data_dir}"
IMAGE="{image}"

mkdir -p "$RUN_DIR/.pbs"
rm -f "$RUN_DIR/.pbs/coinjoin-analysis.done" "$RUN_DIR/.pbs/coinjoin-analysis.failed"
on_exit() {{
  status=$?
  trap - EXIT TERM
  if [ "$status" -eq 0 ]; then
    echo done > "$RUN_DIR/.pbs/coinjoin-analysis.done"
  else
    echo failed > "$RUN_DIR/.pbs/coinjoin-analysis.failed"
  fi
  exit "$status"
}}
trap on_exit EXIT
trap 'exit 143' TERM

test -n "$SCRATCHDIR" || {{ echo "SCRATCHDIR is not set"; exit 1; }}

export SINGULARITY_CACHEDIR="$SCRATCHDIR"
export SINGULARITY_TMPDIR="$SCRATCHDIR"
export SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"

mkdir -p "$OUTPUT_DIR"
mkdir -p "$RUN_DIR/logs"
CONTAINER_WORK_ROOT="$SCRATCHDIR/coinjoin-analysis-selected"
mkdir -p "$CONTAINER_WORK_ROOT/$(basename "$RUN_DIR")"

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
