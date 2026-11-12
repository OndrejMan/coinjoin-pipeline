# shellcheck shell=bash
# Pod evidence and cluster readiness for the k3d tests that run a whole emulation
# (test-kubernetes-pbs-analysis.sh and its --parallel variant).
#
#   source tests/support/kubernetes-pods.sh
#   watch_pod_failures &  POD_WATCHER_PID=$!   # record pods while they still exist
#   stop_pod_watcher
#   wait_for_kubernetes_nodes                  # after `k3d cluster create`
#   dump_kubernetes_diagnostics                # on failure
#
# The caller sets HOST_KUBECONFIG, NAMESPACE, CLUSTER_NAME, WORK_ROOT,
# POD_FAILURE_FILE, KUBERNETES_DIAGNOSTICS_FILE and POD_WATCHER_PID="".

# The emulator tears its namespace down as soon as a run fails, so a pod that
# died mid-run is already gone by the time the post-mortem dump runs - exactly
# the pod whose logs and termination reason are needed. Record the evidence
# while it still exists.
watch_pod_failures() {
  local seen=" " name phase
  while :; do
    while read -r name phase; do
      [[ -n "${name}" ]] || continue
      [[ "${phase}" == "Running" || "${phase}" == "Pending" ]] && continue
      [[ "${seen}" == *" ${name} "* ]] && continue
      seen+="${name} "
      {
        echo "===== ${name} left Running (phase=${phase}) at $(TZ=UTC date -Is) ====="
        kubectl --kubeconfig "${HOST_KUBECONFIG}" describe pod -n "${NAMESPACE}" \
          "${name}" || true
        echo "===== final 500 log lines: ${name} ====="
        kubectl --kubeconfig "${HOST_KUBECONFIG}" logs -n "${NAMESPACE}" "${name}" \
          --all-containers --tail=500 --timestamps || true
      } >>"${POD_FAILURE_FILE}" 2>&1
    done < <(kubectl --kubeconfig "${HOST_KUBECONFIG}" get pods -n "${NAMESPACE}" \
      -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' \
      2>/dev/null || true)
    sleep "${POD_WATCH_INTERVAL_SECONDS:-5}"
  done
}

stop_pod_watcher() {
  [[ -n "${POD_WATCHER_PID}" ]] || return 0
  kill "${POD_WATCHER_PID}" >/dev/null 2>&1 || true
  wait "${POD_WATCHER_PID}" >/dev/null 2>&1 || true
  POD_WATCHER_PID=""
}

# k3d can report a cluster as created while the embedded k3s API server is
# still settling.  A one-off ServiceUnavailable from the first kubectl call is
# transient in that case; do not discard an otherwise healthy test run.
wait_for_kubernetes_nodes() {
  local timeout_seconds="${K3D_NODE_READY_TIMEOUT:-240}"
  local deadline=$((SECONDS + timeout_seconds))
  local remaining

  until (( SECONDS >= deadline )); do
    if kubectl --kubeconfig "${HOST_KUBECONFIG}" get --raw='/readyz' >/dev/null 2>&1; then
      remaining=$((deadline - SECONDS))
      if kubectl --kubeconfig "${HOST_KUBECONFIG}" wait node --all \
        --for=condition=Ready --timeout="${remaining}s"
      then
        return 0
      fi
    fi
    sleep 2
  done

  echo "FAIL: Kubernetes API or all nodes did not become Ready within ${timeout_seconds}s" >&2
  kubectl --kubeconfig "${HOST_KUBECONFIG}" get nodes -o wide >&2 || true
  echo "===== k3s control-plane logs =====" >&2
  docker logs --tail 200 "k3d-${CLUSTER_NAME}-server-0" >&2 || true
  return 1
}

# Everything the namespace still shows, kept in KUBERNETES_DIAGNOSTICS_FILE and
# printed to stderr.
dump_kubernetes_diagnostics() {
  {
    if [[ -s "${POD_FAILURE_FILE}" ]]; then
      echo "===== pods that stopped running during the run (captured live) ====="
      cat "${POD_FAILURE_FILE}"
    else
      echo "No pod left the Running phase while the pod watcher was active."
    fi
    echo "Kubernetes workflow failed; collecting diagnostics for namespace ${NAMESPACE}..."
    if [[ ! -s "${HOST_KUBECONFIG}" ]]; then
      echo "Kubeconfig is unavailable: ${HOST_KUBECONFIG}"
    else
      kubectl --kubeconfig "${HOST_KUBECONFIG}" get pods -n "${NAMESPACE}" -o wide || true
      kubectl --kubeconfig "${HOST_KUBECONFIG}" get services -n "${NAMESPACE}" -o wide || true
      kubectl --kubeconfig "${HOST_KUBECONFIG}" get endpoints -n "${NAMESPACE}" -o wide || true
      echo "===== namespace events ====="
      kubectl --kubeconfig "${HOST_KUBECONFIG}" get events -n "${NAMESPACE}" \
        --sort-by=.metadata.creationTimestamp || true

      local pod
      while IFS= read -r pod; do
        [[ -n "${pod}" ]] || continue
        echo "===== description and events: ${pod} ====="
        kubectl --kubeconfig "${HOST_KUBECONFIG}" describe -n "${NAMESPACE}" \
          "${pod}" || true
        echo "===== final 200 log lines: ${pod} ====="
        kubectl --kubeconfig "${HOST_KUBECONFIG}" logs -n "${NAMESPACE}" \
          "${pod}" --all-containers --tail=200 --timestamps || true
        echo "===== previous final 200 log lines: ${pod} ====="
        local previous_log="${WORK_ROOT}/$(basename "${pod}")-previous.log"
        if kubectl --kubeconfig "${HOST_KUBECONFIG}" logs -n "${NAMESPACE}" \
          "${pod}" --all-containers --tail=200 --timestamps --previous \
          >"${previous_log}" 2>&1
        then
          cat "${previous_log}"
        else
          echo "No previous terminated container logs are available."
        fi
      done < <(kubectl --kubeconfig "${HOST_KUBECONFIG}" get pods -n "${NAMESPACE}" \
        -o name 2>/dev/null || true)
    fi
  } >"${KUBERNETES_DIAGNOSTICS_FILE}" 2>&1
  cat "${KUBERNETES_DIAGNOSTICS_FILE}" >&2
}
