# Embedded into each submitted script; no second download is needed to handle failures.
# Empties the job's scratch once the log and markers are in S3; MetaCentrum would otherwise
# keep it for 14 days. Only the contents go: the system removes the directory itself.
clean_stage_scratch() {
  if [ -z "${SCRATCHDIR:-}" ] || [ ! -d "$SCRATCHDIR" ]; then
    return 0
  fi
  if command -v clean_scratch >/dev/null 2>&1; then
    clean_scratch
  else
    find "$SCRATCHDIR" -mindepth 1 -delete
  fi
}
on_exit() {
  status=$?
  trap - EXIT TERM
  set +e
  upload_status=0
  stage_finalize
  if [ "$status" -eq 0 ] && [ "$upload_status" -ne 0 ]; then
    status=$upload_status
  fi
  if [ "$status" -eq 0 ]; then
    printf 'done\n' > "$DONE_MARKER"
    publish_done || status=$?
  fi
  if [ "$status" -ne 0 ]; then
    printf 'failed\n' > "$FAILED_MARKER"
    publish_failed
  fi
  clean_stage_scratch
  exit "$status"
}
trap on_exit EXIT
trap 'exit 143' TERM
