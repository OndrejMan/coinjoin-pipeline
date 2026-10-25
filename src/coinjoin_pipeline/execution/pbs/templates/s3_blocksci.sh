{preamble}
{start}
require_zstd
s5 sync "$ARTIFACT_URI/$RUN_ID/bitcoin_data/*" "$RUN_WORK/bitcoin_data/"
s5 sync "$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/*" "$RUN_WORK/coinjoin_emulator_data/"
s5 sync "$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*" "$RUN_WORK/.pipeline/exporters/"
BITCOIN_DATADIR="$RUN_WORK/bitcoin_data"
if [ ! -d "$BITCOIN_DATADIR/regtest/blocks" ] && [ -d "$BITCOIN_DATADIR/data/regtest/blocks" ]; then
  BITCOIN_DATADIR="$BITCOIN_DATADIR/data"
fi
test -d "$BITCOIN_DATADIR/regtest/blocks" || {{
  echo "BlockSci S3-compatible reporting requires a Bitcoin datadir containing regtest/blocks; checked $RUN_WORK/bitcoin_data and $RUN_WORK/bitcoin_data/data" >&2
  exit 1
}}
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
s5 sync "$RUN_WORK/logs/" "$ARTIFACT_URI/$RUN_ID/logs/"
