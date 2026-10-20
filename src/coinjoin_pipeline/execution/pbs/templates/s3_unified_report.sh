{preamble}
REPORT_DIR="$RUN_WORK/coinjoinPipeline_data"
mkdir -p \
  "$RUN_WORK/.pipeline/exporters" \
  "$RUN_WORK/coinjoin_emulator_data" \
  "$RUN_WORK/coinjoin-analysis_data" \
  "$RUN_WORK/blocksci-analysis_data" \
  "$RUN_WORK/coinjoin-mappings_data"
{start}
echo "[unified-report] downloading lightweight report inputs"
{download_inputs}
require_file "$RUN_WORK/blocksci-analysis_data/blocksci_analysis.json" \
  "Unified S3 report requires blocksci-analysis_data/blocksci_analysis.json"
require_file "$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json" \
  "Unified S3 report requires coinjoin-analysis_data/coinjoin_tx_info.json"
require_file "$RUN_WORK/.pipeline/exporters/unified_report.py" \
  "Unified S3 report requires .pipeline/exporters/unified_report.py"
echo "[unified-report] assembling JSON and Markdown from precomputed analyzer outputs"
singularity exec \
  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \
  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" \
  --env PBS_RUN_ID="$RUN_ID" "$IMAGE" \
  bash -c 'cd "/runs/emulation/logs/$PBS_RUN_ID" && {command}'
require_file "$REPORT_DIR/unified_report.json" \
  "Unified S3 report did not produce coinjoinPipeline_data/unified_report.json"
s5 sync "$REPORT_DIR/" "$ARTIFACT_URI/$RUN_ID/coinjoinPipeline_data/"
echo "[unified-report] report upload complete"
