#!/bin/bash
#PBS -N {job_name}
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
#PBS -o {run_dir}/logs/{stage}.pbs.log

set -euo pipefail

cd "$PBS_O_WORKDIR"

RUN_DIR="{run_dir}"
RUNS_ROOT="{logs_root}"
BITCOIN_DATADIR="{bitcoin_datadir}"
EXPORTERS_DIR="{exporters_dir}"
IMAGE="{image}"

mkdir -p "$RUN_DIR/.pbs" "$RUN_DIR/logs"
rm -f "$RUN_DIR/.pbs/{stage}.done" "$RUN_DIR/.pbs/{stage}.failed"
on_exit() {{
  status=$?
  trap - EXIT TERM
  if [ "$status" -eq 0 ]; then
    echo done > "$RUN_DIR/.pbs/{stage}.done"
  else
    echo failed > "$RUN_DIR/.pbs/{stage}.failed"
  fi
  exit "$status"
}}
trap on_exit EXIT
trap 'exit 143' TERM

test -n "$SCRATCHDIR" || {{ echo "SCRATCHDIR is not set"; exit 1; }}

export SINGULARITY_CACHEDIR="$SCRATCHDIR"
export SINGULARITY_TMPDIR="$SCRATCHDIR"
export SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"

mkdir -p "$RUN_DIR/blocksci_data"
mkdir -p "$RUN_DIR/blocksciEmulatorAnalysis_data"

# Discover the highest exported block to bound the parser.
EXPORTED_MAX_BLOCK="$(find "$RUN_DIR/coinjoin_emulator_data/data/btc-node" \
  -maxdepth 1 -type f -name 'block_*.json' -printf '%f\n' | \
  sed -nE 's/^block_([0-9]+)\.json$/\1/p' | sort -n | tail -n 1)"
test -n "$EXPORTED_MAX_BLOCK"

cat > "$RUN_DIR/.pbs/{stage}-stage.sh" <<'PBS_BLOCKSCI_STAGE'
#!/bin/bash
set -euo pipefail
cd /runs/emulation/logs/"${{PBS_RUN_ID}}"
EXPORTED_MAX_BLOCK="${{PBS_EXPORTED_MAX_BLOCK}}"
{blocksci_command}
PBS_BLOCKSCI_STAGE
chmod 700 "$RUN_DIR/.pbs/{stage}-stage.sh"

singularity exec \
  --bind /storage:/storage \
  --bind "$RUNS_ROOT:/runs/emulation/logs" \
  --bind "$BITCOIN_DATADIR:/mnt/data:ro" \
  --bind "$EXPORTERS_DIR:/mnt/exporters:ro" \
  --env PBS_RUN_ID="$(basename "$RUN_DIR")" \
  --env PBS_EXPORTED_MAX_BLOCK="$EXPORTED_MAX_BLOCK" \
  "$IMAGE" \
  bash "/runs/emulation/logs/$(basename "$RUN_DIR")/.pbs/{stage}-stage.sh"
