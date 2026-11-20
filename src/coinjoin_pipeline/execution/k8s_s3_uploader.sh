mkdir -p "/artifacts/$RUN_ID/.k8s"
write_s3_credentials
# emulation_logs.zip is a byte-for-byte copy of coinjoin_emulator_data, which is uploaded anyway,
# and nothing reads it from S3. s5cmd 2.3.0 does not match --exclude relative to the source
# directory, so the pattern starts with a wildcard.
sync_run() {
  s5 sync --exclude '*/coinjoin_emulator_data/emulation_logs.zip' "/artifacts/$RUN_ID/" "$ARTIFACT_URI/$RUN_ID/"
}
# A deadline, not a countdown of the sleeps: the two kubectl calls per poll take time too.
# BusyBox sh has no $SECONDS, so the clock comes from date.
deadline=$(($(date +%s) + EMULATION_TIMEOUT_SECONDS))
while [ ! -f /artifacts/.controller.done ] && [ ! -f /artifacts/.controller.failed ]; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    printf 'controller exceeded emulation timeout (%ss)\n' "$EMULATION_TIMEOUT_SECONDS" >&2
    printf 'failed\n' > /artifacts/.controller.failed
    break
  fi
  terminated_exit="$(kubectl --namespace "$NAMESPACE" get pod "$POD_NAME" \
    -o 'jsonpath={.status.containerStatuses[?(@.name=="controller")].state.terminated.exitCode}' \
    2>/dev/null || true)"
  waiting_reason="$(kubectl --namespace "$NAMESPACE" get pod "$POD_NAME" \
    -o 'jsonpath={.status.containerStatuses[?(@.name=="controller")].state.waiting.reason}' \
    2>/dev/null || true)"
  if [ -n "$terminated_exit" ]; then
    printf 'controller terminated without completion marker (exit %s)\n' "$terminated_exit" >&2
    printf 'failed\n' > /artifacts/.controller.failed
    break
  fi
  case "$waiting_reason" in
    ErrImagePull|ImagePullBackOff|InvalidImageName|CreateContainerConfigError)
      printf 'controller failed to start: %s\n' "$waiting_reason" >&2
      printf 'failed\n' > /artifacts/.controller.failed
      break
      ;;
  esac
  sleep 2
done
if [ -f /artifacts/.controller.failed ]; then
  printf 'failed\n' > "/artifacts/$RUN_ID/.k8s/upload.failed"
  s5 cp "/artifacts/$RUN_ID/.k8s/upload.failed" "$ARTIFACT_URI/$RUN_ID/.k8s/upload.failed" || true
  sync_run || true
  exit 1
fi
sync_run
printf 'done\n' > "/artifacts/$RUN_ID/.k8s/upload.done"
s5 cp "/artifacts/$RUN_ID/.k8s/upload.done" "$ARTIFACT_URI/$RUN_ID/.k8s/upload.done"
