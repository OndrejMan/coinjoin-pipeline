{preamble}
IMAGE="{image}"
RUNS_ROOT="{logs_root}"
BITCOIN_DATADIR="{bitcoin_datadir}"
EXPORTERS_DIR="{exporters_dir}"
mkdir -p "$RUN_DIR/blocksci_data" "$RUN_DIR/coinjoinPipeline_data"

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
{command}
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
