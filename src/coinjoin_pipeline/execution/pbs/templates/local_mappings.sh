{preamble}
ENUMERATOR_IMAGE={enumerator_image}
SAKE_IMAGE={sake_image}
WORK="$RUN_DIR"
OUT="$RUN_DIR/coinjoin-mappings_data"
INPUT="$RUN_DIR/coinjoin-analysis_data/coinjoin_tx_info.json"
mkdir -p "$OUT"
{mappings_run}
