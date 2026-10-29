{preamble}
ENUMERATOR_IMAGE={enumerator_image}
SAKE_IMAGE={sake_image}
WORK="$RUN_WORK"
OUT="$RUN_WORK/coinjoin-mappings_data"
mkdir -p "$RUN_WORK/coinjoin-analysis_data" "$OUT"
stage_finalize() {{
  if [ -d "$OUT" ]; then
    s5 sync "$OUT/" "$RUN_PREFIX/coinjoin-mappings_data/" || upload_status=$?
  fi
}}
{start}

echo "[coinjoin-mappings] downloading coinjoin-analysis input"
s5 sync "$RUN_PREFIX/coinjoin-analysis_data/*" "$RUN_WORK/coinjoin-analysis_data/"
INPUT="$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json"
require_file "$INPUT" "CoinJoin mappings require coinjoin-analysis_data/coinjoin_tx_info.json"
{mappings_run}
echo "[coinjoin-mappings] completed"
