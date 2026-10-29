{preamble}
RESULT_ID="$(date -u +%Y%m%dT%H%M%SZ)-${{PBS_JOBID:-manual}}"
OUTPUT="$RUN_WORK/{output_directory}/{program}/$RESULT_ID"
mkdir -p "$OUTPUT/input"
# Keep logs, the invocation and partial outputs even when the analyzer fails.
stage_finalize() {{
  s5 sync "$OUTPUT/" "$RUN_PREFIX/{output_directory}/{program}/$RESULT_ID/" || upload_status=$?
}}
{start}
{download}
cat > "$OUTPUT/invocation.json" <<'COINJOIN_INVOCATION'
{invocation}
COINJOIN_INVOCATION
{adapter}
singularity exec --cleanenv \
  --bind "$OUTPUT:/work:rw" \
  --env MPLBACKEND=Agg --env MPLCONFIGDIR=/tmp/matplotlib \
  "$IMAGE" bash -c {command} 2>&1 | tee "$OUTPUT/analysis.log"
