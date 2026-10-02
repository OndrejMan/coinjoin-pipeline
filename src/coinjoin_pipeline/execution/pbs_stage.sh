# Embedded into each submitted script; no second download is needed to handle failures.
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
  exit "$status"
}
trap on_exit EXIT
trap 'exit 143' TERM
