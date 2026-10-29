{preamble}
{start}
require_zstd
s5 sync "$RUN_PREFIX/bitcoin_data/*" "$RUN_WORK/bitcoin_data/"
s5 sync "$RUN_PREFIX/coinjoin_emulator_data/*" "$RUN_WORK/coinjoin_emulator_data/"
s5 sync "$RUN_PREFIX/.pipeline/exporters/*" "$RUN_WORK/.pipeline/exporters/"
locate_emulator_bitcoin_datadir
test -d "$RUN_WORK/.pipeline/exporters"
{before}
EXPORTED_MAX_BLOCK="$(emulator_max_block)"
test -n "$EXPORTED_MAX_BLOCK"
singularity exec \
  --cleanenv \
  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \
  --bind "$BITCOIN_DATADIR:/mnt/data:ro" \
  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" \
  --env PBS_RUN_ID="$RUN_ID" --env PBS_EXPORTED_MAX_BLOCK="$EXPORTED_MAX_BLOCK" "$IMAGE" \
  bash -c 'cd "/runs/emulation/logs/$PBS_RUN_ID" && EXPORTED_MAX_BLOCK="$PBS_EXPORTED_MAX_BLOCK" && {command}'
{after}
s5 sync "$RUN_WORK/logs/" "$LOG_PREFIX/"
