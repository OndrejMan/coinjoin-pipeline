mkdir -p "/artifacts/$RUN_ID/.k8s"
write_s3_credentials
remaining="$EMULATION_TIMEOUT_SECONDS"
while [ ! -f /artifacts/.controller.done ] && [ ! -f /artifacts/.controller.failed ]; do
  if [ "$remaining" -le 0 ]; then
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
  remaining=$((remaining - 2))
done
if [ -f /artifacts/.controller.failed ]; then
  printf 'failed\n' > "/artifacts/$RUN_ID/.k8s/upload.failed"
  s5 cp "/artifacts/$RUN_ID/.k8s/upload.failed" "$ARTIFACT_URI/$RUN_ID/.k8s/upload.failed" || true
  s5 sync "/artifacts/$RUN_ID/" "$ARTIFACT_URI/$RUN_ID/" || true
  exit 1
fi
s5 sync "/artifacts/$RUN_ID/" "$ARTIFACT_URI/$RUN_ID/"
printf 'done\n' > "/artifacts/$RUN_ID/.k8s/upload.done"
s5 cp "/artifacts/$RUN_ID/.k8s/upload.done" "$ARTIFACT_URI/$RUN_ID/.k8s/upload.done"
