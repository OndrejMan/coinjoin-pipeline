EMULATION_LOGS_DIR=/storage/logs EXPORTERS_DIR=/storage/exporters uv run python3 blocksciEmulatorAnalysis/client/wrapper.py analyze \
  --engine joinmarket \
  --run-dir my-test-run \
  --blocksciPbs \
  --pbs-bitcoin-datadir /storage/btc-data \
  --dry-run
