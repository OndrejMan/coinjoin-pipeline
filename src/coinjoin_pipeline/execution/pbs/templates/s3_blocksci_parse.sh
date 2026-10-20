{preamble}
CACHE_DIR="$RUN_WORK/blocksci-parse_data"
mkdir -p "$RUN_WORK/coinjoin_emulator_data/data/btc-node" "$RUN_WORK/.pipeline/exporters" "$CACHE_DIR"
JOB_LOG="$RUN_WORK/logs/blocksci-parse.pbs.log"
stage_finalize() {{
  exec 1>&3 2>&4
  exec 3>&- 4>&-
  s5 cp "$JOB_LOG" "$ARTIFACT_URI/$RUN_ID/logs/blocksci-parse.pbs.log" || upload_status=$?
  {finalize_index}
}}
exec 3>&1 4>&2
exec >"$JOB_LOG" 2>&1
{start}
require_zstd
s5 sync "$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*" "$RUN_WORK/.pipeline/exporters/"
echo "[blocksci-parse] preparing {source_description}"
MANIFEST_EXTRA=""
{prepare_source}
{produce_index}
require_file "$RUN_WORK/blocksci_data/config.json" "BlockSci parser did not produce blocksci_data/config.json"
require_file "$RUN_WORK/blocksci_data/parsed/chain/block.dat" "BlockSci parser did not produce parsed/chain/block.dat"
{verify_index}
{publish_cache}
echo "[blocksci-parse] reusable cache upload complete"
