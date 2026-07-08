#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PBS_SUPPORT_ROOT="${PBS_SUPPORT_ROOT:-${SCRIPT_DIR}/support/pbs}"
PBS_HELPER="${PBS_HELPER:-${PBS_SUPPORT_ROOT}/local-pbs.sh}"
PBS_ENV="${PBS_ENV:-${PBS_SUPPORT_ROOT}/pbs-env.sh}"
ENGINE="${1:-all}"

if [[ "${ENGINE}" == "all" ]]; then
  "${BASH_SOURCE[0]}" wasabi
  "${BASH_SOURCE[0]}" joinmarket
  exit 0
fi
if [[ "${ENGINE}" != "wasabi" && "${ENGINE}" != "joinmarket" ]]; then
  echo "Usage: $0 [all|wasabi|joinmarket]" >&2
  exit 2
fi

for command in docker k3d kubectl python3 timeout; do
  command -v "${command}" >/dev/null 2>&1 || {
    echo "FAIL: required command not found: ${command}" >&2
    exit 2
  }
done
docker info >/dev/null 2>&1 || {
  echo "FAIL: Docker daemon is not reachable by the current user." >&2
  exit 2
}
[[ -x "${PBS_HELPER}" ]] || { echo "FAIL: PBS helper not found: ${PBS_HELPER}" >&2; exit 2; }
[[ -f "${PBS_ENV}" ]] || { echo "FAIL: PBS environment not found: ${PBS_ENV}" >&2; exit 2; }

RUN_TOKEN="$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM}"
RESOURCE_ID="${GITHUB_RUN_ID:-$$}"
case "${ENGINE}" in
  wasabi) ENGINE_CLUSTER_ID="w" ;;
  joinmarket) ENGINE_CLUSTER_ID="jm" ;;
esac
STORAGE_BASE="${PBS_TEST_STORAGE_ROOT:-/storage/gitlab-runner}"
[[ -d "${STORAGE_BASE}" && -w "${STORAGE_BASE}" ]] || {
  echo "FAIL: pre-provisioned writable storage is required: ${STORAGE_BASE}" >&2
  exit 2
}
WORK_ROOT="$(mktemp -d "${STORAGE_BASE}/k3d-pbs-${ENGINE}-${RUN_TOKEN}.XXXXXX")"
LOGS_ROOT="${WORK_ROOT}/emulation_logs"
BITCOIN_DATADIR="${WORK_ROOT}/bitcoin-regtest-data"
CLUSTER_NAME="${CLUSTER_NAME:-cj-${ENGINE_CLUSTER_ID}-pbs-${RESOURCE_ID}}"
NAMESPACE="${NAMESPACE:-cj-${ENGINE}-pbs-$$}"
PBS_CONTAINER_NAME="${PBS_CONTAINER_NAME:-pbs-${ENGINE}-itest-${RESOURCE_ID}}"
HOST_KUBECONFIG="${WORK_ROOT}/kubeconfig-host.yaml"
CONTAINER_KUBECONFIG="${WORK_ROOT}/kubeconfig-container.yaml"
IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/ondrejman/}"
WRAPPER_IMAGE="${WRAPPER_IMAGE:-ghcr.io/ondrejman/coinjoin-pipeline:latest}"
COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"
RESULT_DIR="${TEST_RESULT_DIR:-}"
KEEP_WORK="${KEEP_TEST_WORK:-0}"
KUBERNETES_PBS_TIMEOUT="${KUBERNETES_PBS_TIMEOUT:-85m}"
KUBERNETES_DIAGNOSTICS_FILE="${WORK_ROOT}/kubernetes-diagnostics.txt"
PIPELINE_OUTPUT_FILE="${WORK_ROOT}/pipeline-output.log"
INJECT_KUBERNETES_CLIENT_FAILURE="${INJECT_KUBERNETES_CLIENT_FAILURE:-0}"
INJECT_KUBERNETES_CLIENT_NAME="${INJECT_KUBERNETES_CLIENT_NAME:-wasabi-client-002}"
FAILURE_INJECTOR_PID=""

if [[ "${ENGINE}" == "wasabi" ]]; then
  SCENARIO="${SCENARIO:-overactive-local.json}"
  EXPECTED_SCENARIO="overactive-local"
  EXPECTED_COINJOIN_TYPE="wasabi2"
else
  SCENARIO="${SCENARIO:-defaultJoinMarket.json}"
  EXPECTED_SCENARIO="default-joinmarket"
  EXPECTED_COINJOIN_TYPE="joinmarket"
fi

dump_kubernetes_diagnostics() {
  {
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
        kubectl --kubeconfig "${HOST_KUBECONFIG}" logs -n "${NAMESPACE}" \
          "${pod}" --all-containers --tail=200 --timestamps --previous || true
      done < <(kubectl --kubeconfig "${HOST_KUBECONFIG}" get pods -n "${NAMESPACE}" \
        -o name 2>/dev/null || true)
    fi
  } >"${KUBERNETES_DIAGNOSTICS_FILE}" 2>&1
  cat "${KUBERNETES_DIAGNOSTICS_FILE}" >&2
}

inject_kubernetes_client_failure() {
  local deadline=$((SECONDS + ${INJECT_KUBERNETES_CLIENT_APPEAR_TIMEOUT_SECONDS:-1200}))
  until kubectl --kubeconfig "${HOST_KUBECONFIG}" get pod -n "${NAMESPACE}" \
    "${INJECT_KUBERNETES_CLIENT_NAME}" >/dev/null 2>&1
  do
    if (( SECONDS >= deadline )); then
      echo "FAIL: pod ${INJECT_KUBERNETES_CLIENT_NAME} did not appear before fault injection" >&2
      return 1
    fi
    sleep 2
  done
  kubectl --kubeconfig "${HOST_KUBECONFIG}" wait -n "${NAMESPACE}" \
    --for=condition=Ready "pod/${INJECT_KUBERNETES_CLIENT_NAME}" --timeout=20m
  sleep "${INJECT_KUBERNETES_CLIENT_DELAY_SECONDS:-15}"
  kubectl --kubeconfig "${HOST_KUBECONFIG}" delete pod -n "${NAMESPACE}" \
    "${INJECT_KUBERNETES_CLIENT_NAME}" --wait=false
  touch "${WORK_ROOT}/client-failure-injected"
}

copy_analysis_artifacts() {
  [[ -n "${RESULT_DIR}" && -d "${LOGS_ROOT}" ]] || return 0
  local run_dir=""
  local report_json=""
  local baseline_json=""

  report_json="$(find "${LOGS_ROOT}" -mindepth 3 -maxdepth 3 -type f \
    -path '*/blocksciEmulatorAnalysis_data/unified_report.json' -print 2>/dev/null | sort | tail -n 1)"
  if [[ -n "${report_json}" ]]; then
    run_dir="${report_json%/blocksciEmulatorAnalysis_data/unified_report.json}"
  else
    baseline_json="$(find "${LOGS_ROOT}" -mindepth 3 -maxdepth 3 -type f \
      -path '*/coinjoin-analysis_data/coinjoin_tx_info.json' -print 2>/dev/null | sort | tail -n 1)"
    if [[ -n "${baseline_json}" ]]; then
      run_dir="${baseline_json%/coinjoin-analysis_data/coinjoin_tx_info.json}"
    fi
  fi

  [[ -n "${run_dir}" ]] || return 0
  mkdir -p "${RESULT_DIR}/${ENGINE}"
  [[ -s "${run_dir}/blocksciEmulatorAnalysis_data/unified_report.json" ]] \
    && cp "${run_dir}/blocksciEmulatorAnalysis_data/unified_report.json" "${RESULT_DIR}/${ENGINE}/"
  [[ -s "${run_dir}/blocksciEmulatorAnalysis_data/unified_report.md" ]] \
    && cp "${run_dir}/blocksciEmulatorAnalysis_data/unified_report.md" "${RESULT_DIR}/${ENGINE}/"
  [[ -s "${run_dir}/coinjoin-analysis_data/coinjoin_tx_info.json" ]] \
    && cp "${run_dir}/coinjoin-analysis_data/coinjoin_tx_info.json" "${RESULT_DIR}/${ENGINE}/"
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "${FAILURE_INJECTOR_PID}" ]]; then
    kill "${FAILURE_INJECTOR_PID}" >/dev/null 2>&1 || true
    wait "${FAILURE_INJECTOR_PID}" >/dev/null 2>&1 || true
  fi
  if (( status != 0 )); then
    dump_kubernetes_diagnostics
  fi
  if [[ -n "${RESULT_DIR}" ]]; then
    mkdir -p "${RESULT_DIR}/${ENGINE}/pbs-logs"
    find "${WORK_ROOT}" -type f \( -name '*.o[0-9]*' -o -name '*.e[0-9]*' \) \
      -exec cp -t "${RESULT_DIR}/${ENGINE}/pbs-logs" {} + 2>/dev/null || true
    copy_analysis_artifacts || true
    if [[ -s "${KUBERNETES_DIAGNOSTICS_FILE}" ]]; then
      mkdir -p "${RESULT_DIR}/${ENGINE}/kubernetes-diagnostics"
      cp "${KUBERNETES_DIAGNOSTICS_FILE}" \
        "${RESULT_DIR}/${ENGINE}/kubernetes-diagnostics/diagnostics.txt"
      if [[ -s "${PIPELINE_OUTPUT_FILE}" ]]; then
        cp "${PIPELINE_OUTPUT_FILE}" \
          "${RESULT_DIR}/${ENGINE}/kubernetes-diagnostics/pipeline-output.log"
      fi
    fi
  fi
  docker rm -f "${PBS_CONTAINER_NAME}" >/dev/null 2>&1 || true
  if [[ "${KEEP_CLUSTER:-0}" != 1 ]]; then
    k3d cluster delete "${CLUSTER_NAME}" >/dev/null 2>&1 || true
  fi
  if [[ "${KEEP_WORK}" != 1 ]]; then
    docker run --rm --user root -v "${WORK_ROOT}:/test-work" \
      --entrypoint chmod "${IMAGE_PREFIX}btc-node" -R a+rwX /test-work >/dev/null 2>&1 || true
    rm -rf "${WORK_ROOT}"
  else
    echo "Keeping test work directory: ${WORK_ROOT}" >&2
  fi
  exit "${status}"
}
trap cleanup EXIT

# Remove only resources owned by this test name before attempting to recreate
# them. This recovers safely from an interrupted previous run without touching
# unrelated Docker containers or k3d clusters on the shared runner.
k3d cluster delete "${CLUSTER_NAME}" >/dev/null 2>&1 || true
docker rm -f "${PBS_CONTAINER_NAME}" >/dev/null 2>&1 || true

mkdir -p "${LOGS_ROOT}" "${BITCOIN_DATADIR}"
chmod 0777 "${WORK_ROOT}" "${LOGS_ROOT}" "${BITCOIN_DATADIR}"

CONTAINER_KUBE_HOST="${CONTAINER_KUBE_HOST:-$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')}"
echo "Creating k3d cluster ${CLUSTER_NAME} with direct shared storage ${WORK_ROOT}..."
k3d cluster create "${CLUSTER_NAME}" \
  --servers 1 --agents "${K3D_AGENTS:-2}" --wait --timeout "${K3D_WAIT_TIMEOUT:-240s}" \
  --volume "${WORK_ROOT}:${WORK_ROOT}@all"
k3d kubeconfig get "${CLUSTER_NAME}" >"${HOST_KUBECONFIG}"
kubectl --kubeconfig "${HOST_KUBECONFIG}" wait node --all --for=condition=Ready --timeout=240s

cp "${HOST_KUBECONFIG}" "${CONTAINER_KUBECONFIG}"
API_SERVER="$(kubectl --kubeconfig "${HOST_KUBECONFIG}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
API_PORT="${API_SERVER##*:}"
API_PORT="${API_PORT%%/*}"
KUBE_CLUSTER="$(kubectl --kubeconfig "${CONTAINER_KUBECONFIG}" config view --minify -o jsonpath='{.contexts[0].context.cluster}')"
kubectl --kubeconfig "${CONTAINER_KUBECONFIG}" config set-cluster "${KUBE_CLUSTER}" \
  --server="https://${CONTAINER_KUBE_HOST}:${API_PORT}" --insecure-skip-tls-verify=true >/dev/null
kubectl --kubeconfig "${CONTAINER_KUBECONFIG}" config unset \
  "clusters.${KUBE_CLUSTER}.certificate-authority-data" >/dev/null 2>&1 || true

python3 - "${CLUSTER_NAME}" "${WORK_ROOT}" <<'PY'
import json
import subprocess
import sys

cluster, shared = sys.argv[1:]
nodes = json.loads(subprocess.check_output(["docker", "inspect", f"k3d-{cluster}-server-0"]))
mounts = nodes[0].get("Mounts", [])
if not any(item.get("Source") == shared and item.get("Destination") == shared for item in mounts):
    raise SystemExit(f"FAIL: k3d node does not directly mount shared path {shared}")
PY

export PBS_CONTAINER_NAME PBS_WORKDIR_HOST="${WORK_ROOT}" PBS_WORKDIR_CONTAINER="${WORK_ROOT}"
"${PBS_HELPER}" start
source "${PBS_ENV}"

export PBS_CLIENT_WORKDIR="${WORK_ROOT}"
export PBS_FRONTEND_DIRECT=1
export EMULATION_LOGS_DIR="${LOGS_ROOT}"
export WRAPPER_IMAGE COINJOIN_EMULATOR_IMAGE
export KUBERNETES_CONTROL_IP="${CONTAINER_KUBE_HOST}"
export KUBERNETES_STORAGE_UID="$(id -u)"
export KUBERNETES_STORAGE_GID="$(id -g)"

echo "Running ${ENGINE} Kubernetes emulation followed by PBS analyzers..."
if [[ "${INJECT_KUBERNETES_CLIENT_FAILURE}" == 1 ]]; then
  inject_kubernetes_client_failure &
  FAILURE_INJECTOR_PID=$!
fi

set +e
(
  cd "${PROJECT_DIR}"
  timeout --foreground "${KUBERNETES_PBS_TIMEOUT}" ./runIt.sh full-run \
    --engine "${ENGINE}" \
    --scenario "${SCENARIO}" \
    --driver kubernetes \
    --namespace "${NAMESPACE}" \
    --kubeconfig "${CONTAINER_KUBECONFIG}" \
    --image-prefix "${IMAGE_PREFIX}" \
    --kubernetes-btc-datadir "${BITCOIN_DATADIR}" \
    --analysisPbs \
    --blocksciPbs \
    --pbs-bitcoin-datadir "${BITCOIN_DATADIR}" \
    --pbs-ncpus 2 \
    --pbs-mem 4gb \
    --pbs-scratch 2gb \
    --pbs-walltime 00:30:00
) 2>&1 | tee "${PIPELINE_OUTPUT_FILE}"
PIPELINE_STATUS=${PIPESTATUS[0]}
set -e

if [[ -n "${FAILURE_INJECTOR_PID}" ]]; then
  wait "${FAILURE_INJECTOR_PID}" || true
  FAILURE_INJECTOR_PID=""
fi

if [[ "${INJECT_KUBERNETES_CLIENT_FAILURE}" == 1 ]]; then
  [[ -f "${WORK_ROOT}/client-failure-injected" ]] || {
    echo "FAIL: Kubernetes client failure was not injected" >&2
    exit 1
  }
  (( PIPELINE_STATUS != 0 )) || {
    echo "FAIL: pipeline succeeded after ${INJECT_KUBERNETES_CLIENT_NAME} was deleted" >&2
    exit 1
  }
  dump_kubernetes_diagnostics
  [[ -s "${KUBERNETES_DIAGNOSTICS_FILE}" ]] || {
    echo "FAIL: Kubernetes diagnostics artifact is empty" >&2
    exit 1
  }
  echo "PASS: deleting ${INJECT_KUBERNETES_CLIENT_NAME} failed the pipeline with diagnostics"
  exit 0
fi

(( PIPELINE_STATUS == 0 )) || exit "${PIPELINE_STATUS}"

RUN_DIR="$(find "${LOGS_ROOT}" -mindepth 1 -maxdepth 1 -type d \
  -exec test -s '{}/blocksciEmulatorAnalysis_data/unified_report.json' \; -print | sort | tail -n 1)"
[[ -n "${RUN_DIR}" ]] || { echo "FAIL: no completed report under ${LOGS_ROOT}" >&2; exit 1; }
[[ -s "${BITCOIN_DATADIR}/regtest/blocks/blk00000.dat" ]] || {
  echo "FAIL: Kubernetes did not write the directly mounted Bitcoin datadir" >&2
  exit 1
}

python3 - "${RUN_DIR}" "${EXPECTED_SCENARIO}" "${EXPECTED_COINJOIN_TYPE}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
expected_scenario, expected_type = sys.argv[2:]
report = json.loads((run_dir / "blocksciEmulatorAnalysis_data/unified_report.json").read_text())
baseline = json.loads((run_dir / "coinjoin-analysis_data/coinjoin_tx_info.json").read_text())
run = report.get("run") or {}
summary = report.get("summary") or {}
if run.get("scenario_name") != expected_scenario:
    raise SystemExit(f"FAIL: scenario {run.get('scenario_name')!r} != {expected_scenario!r}")
if run.get("coinjoin_type") != expected_type:
    raise SystemExit(f"FAIL: coinjoin type {run.get('coinjoin_type')!r} != {expected_type!r}")
if not baseline:
    raise SystemExit("FAIL: coinjoin-analysis produced no records")
if summary.get("blocksci_detected_coinjoins", 0) < 1:
    raise SystemExit("FAIL: BlockSci detected no CoinJoin transactions")
if "blocksci_agreement_rate" not in summary:
    raise SystemExit("FAIL: report has no analyzer agreement metrics")
if expected_type == "joinmarket":
    events = run_dir / "coinjoin_emulator_data/data/joinmarket_round_events.json"
    if not events.is_file():
        raise SystemExit("FAIL: JoinMarket round events are missing")
print(
    f"PASS: {expected_type} via Kubernetes→shared storage→PBS; "
    f"baseline={len(baseline)}, blocksci={summary['blocksci_detected_coinjoins']}"
)
PY

if [[ -n "${RESULT_DIR}" ]]; then
  mkdir -p "${RESULT_DIR}/${ENGINE}"
  cp "${RUN_DIR}/blocksciEmulatorAnalysis_data/unified_report.json" "${RESULT_DIR}/${ENGINE}/"
  cp "${RUN_DIR}/blocksciEmulatorAnalysis_data/unified_report.md" "${RESULT_DIR}/${ENGINE}/"
  cp "${RUN_DIR}/coinjoin-analysis_data/coinjoin_tx_info.json" "${RESULT_DIR}/${ENGINE}/"
fi
