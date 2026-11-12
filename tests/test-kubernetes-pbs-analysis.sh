#!/usr/bin/env bash
set -euo pipefail

# Kubernetes runs the emulation; --analysis-pbs --blocksci-pbs schedule both
# analyzers and the unified report on PBS over the shared storage.
# With E2E_PARALLEL=1 (test-parallel-pbs-analysis.sh) the run adds --parallel:
# both analyzers must run concurrently under the wrapper's "Parallel analysis"
# pipeline stage and the report must start only after both finished.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=tests/support/local-images.sh
source "${SCRIPT_DIR}/support/local-images.sh"
# shellcheck source=tests/support/kubernetes-pods.sh
source "${SCRIPT_DIR}/support/kubernetes-pods.sh"
# shellcheck source=tests/support/pbs-images.sh
source "${SCRIPT_DIR}/support/pbs-images.sh"
E2E_PARALLEL="${E2E_PARALLEL:-0}"
# The parallel variant keeps its own resource names: the cleanup scripts and
# the suite watcher recognise its clusters by them (cj-<engine>-pbsp-<id>).
if [[ "${E2E_PARALLEL}" == 1 ]]; then
  VARIANT_SUFFIX="-parallel"
  CLUSTER_KIND="pbsp"
  RESULT_NAME="parallel-pbs-analysis"
  PARALLEL_ARGS=(--parallel)
  PBS_DESCRIPTION="parallel PBS"
else
  VARIANT_SUFFIX=""
  CLUSTER_KIND="pbs"
  RESULT_NAME="kubernetes-pbs-analysis"
  PARALLEL_ARGS=()
  PBS_DESCRIPTION="PBS"
fi
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

RUN_TOKEN="$(TZ=Europe/Prague date +%Y%m%dT%H%M%S%Z)-$$-${RANDOM}"
RESOURCE_ID="${GITHUB_RUN_ID:-$$}"
case "${ENGINE}" in
  wasabi) ENGINE_CLUSTER_ID="w" ;;
  joinmarket) ENGINE_CLUSTER_ID="jm" ;;
esac
STORAGE_BASE="${PBS_TEST_STORAGE_ROOT:-/storage/github-runner}"
[[ -d "${STORAGE_BASE}" && -w "${STORAGE_BASE}" ]] || {
  echo "FAIL: pre-provisioned writable storage is required: ${STORAGE_BASE}" >&2
  exit 2
}
WORK_ROOT="$(mktemp -d "${STORAGE_BASE}/k3d-pbs${VARIANT_SUFFIX}-${ENGINE}-${RUN_TOKEN}.XXXXXX")"
LOGS_ROOT="${WORK_ROOT}/emulation_logs"
BITCOIN_DATADIR="${WORK_ROOT}/bitcoin-regtest-data"
CLUSTER_NAME="${CLUSTER_NAME:-cj-${ENGINE_CLUSTER_ID}-${CLUSTER_KIND}-${RESOURCE_ID}}"
NAMESPACE="${NAMESPACE:-cj-${ENGINE}-pbs${VARIANT_SUFFIX}-$$}"
PBS_CONTAINER_NAME="${PBS_CONTAINER_NAME:-pbs-${ENGINE}${VARIANT_SUFFIX}-itest-${RESOURCE_ID}}"
HOST_KUBECONFIG="${WORK_ROOT}/kubeconfig-host.yaml"
CONTAINER_KUBECONFIG="${WORK_ROOT}/kubeconfig-container.yaml"
IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/ondrejman/}"
COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/emulator-manager:latest}"
RESULT_DIR="${TEST_RESULT_DIR:-${PROJECT_DIR}/emulation_logs/_test-results/${RESULT_NAME}-${RUN_TOKEN}}"
KEEP_WORK="${KEEP_TEST_WORK:-0}"
KUBERNETES_PBS_TIMEOUT="${KUBERNETES_PBS_TIMEOUT:-240m}"
KUBERNETES_DIAGNOSTICS_FILE="${WORK_ROOT}/kubernetes-diagnostics.txt"
POD_FAILURE_FILE="${WORK_ROOT}/pod-failures.txt"
PIPELINE_OUTPUT_FILE="${WORK_ROOT}/pipeline-output.log"
FAILED_LOGS_DIR="${LOGS_ROOT}/_failed"
INJECT_KUBERNETES_CLIENT_FAILURE="${INJECT_KUBERNETES_CLIENT_FAILURE:-0}"
INJECT_KUBERNETES_CLIENT_NAME="${INJECT_KUBERNETES_CLIENT_NAME:-wasabi-client-002}"
FAILURE_INJECTOR_PID=""
POD_WATCHER_PID=""
# Optional offline mode: run a PBS analyzer from a local Docker image
# (tests/support/pbs-images.sh).
PBS_BLOCKSCI_LOCAL_IMAGE="${PBS_BLOCKSCI_LOCAL_IMAGE:-}"
PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE="${PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE:-}"
PBS_IMAGE_ARGS=()

if [[ "${ENGINE}" == "wasabi" ]]; then
  SCENARIO="${SCENARIO:-overactive-local.json}"
  EXPECTED_SCENARIO="overactive-local"
  EXPECTED_COINJOIN_TYPE="wasabi2"
  MIN_INPUT_COUNT_ARGS=(--min-input-count 15)
else
  SCENARIO="${SCENARIO:-defaultJoinMarket.json}"
  EXPECTED_SCENARIO="default-joinmarket"
  EXPECTED_COINJOIN_TYPE="joinmarket"
  MIN_INPUT_COUNT_ARGS=()
fi

# A PBS stage reports only that it failed; the reason is in the job's own
# output under the run directory, which lives in the scratch work root that
# cleanup is about to delete. Archive it alongside the pipeline log.
archive_pbs_stage_artifacts() {
  local timestamp="$1" log run_id marker markers
  [[ -d "${LOGS_ROOT}" ]] || return 0
  while IFS= read -r log; do
    [[ -s "${log}" ]] || continue
    run_id="$(basename "$(dirname "$(dirname "${log}")")")"
    cp "${log}" \
      "${FAILED_LOGS_DIR}/${timestamp}-${run_id}-$(basename "${log}")" || true
  done < <(find "${LOGS_ROOT}" -mindepth 3 -maxdepth 3 -type f -name '*.pbs.log' \
    2>/dev/null | sort)

  # Which stages reached which marker: this says what the pipeline was waiting
  # on when it gave up, even for a stage that never produced a log.
  markers="${FAILED_LOGS_DIR}/${timestamp}-kubernetes-pbs-markers.txt"
  while IFS= read -r marker; do
    printf '%s: %s\n' "${marker#"${LOGS_ROOT}"/}" \
      "$(head -c 200 "${marker}" | tr '\n' ' ')"
  done < <(find "${LOGS_ROOT}" -mindepth 3 -maxdepth 3 -type f -path '*/.pbs/*' \
    \( -name '*.done' -o -name '*.failed' -o -name '*.jobid' \) 2>/dev/null | sort) \
    >"${markers}"
  [[ -s "${markers}" ]] || rm -f "${markers}"
}

archive_failure_artifacts() {
  local timestamp
  timestamp="$(TZ=UTC date +%Y%m%dT%H%M%S.%NZ)"
  mkdir -p "${FAILED_LOGS_DIR}"
  # A missing pipeline log must not abort the rest of the archival under set -e.
  [[ -s "${PIPELINE_OUTPUT_FILE}" ]] && cp "${PIPELINE_OUTPUT_FILE}" \
    "${FAILED_LOGS_DIR}/${timestamp}-kubernetes-pbs-pipeline.log" || true
  [[ -s "${KUBERNETES_DIAGNOSTICS_FILE}" ]] && cp "${KUBERNETES_DIAGNOSTICS_FILE}" \
    "${FAILED_LOGS_DIR}/${timestamp}-kubernetes-pbs-diagnostics.log" || true
  archive_pbs_stage_artifacts "${timestamp}" || true
  # The Kubernetes dump above describes a namespace that is already torn down, so
  # it reads as "nothing wrong here" even when a later stage is what failed. Point
  # at the archived pipeline and PBS logs, which carry the actual reason.
  echo "Failure artifacts archived under ${FAILED_LOGS_DIR} (prefix ${timestamp});" \
    "the failing stage and its reason are in ${timestamp}-kubernetes-pbs-pipeline.log" >&2
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
    -path '*/coinjoinPipeline_data/unified_report.json' -print 2>/dev/null | sort | tail -n 1)"
  if [[ -n "${report_json}" ]]; then
    run_dir="${report_json%/coinjoinPipeline_data/unified_report.json}"
  else
    baseline_json="$(find "${LOGS_ROOT}" -mindepth 3 -maxdepth 3 -type f \
      -path '*/coinjoin-analysis_data/coinjoin_tx_info.json' -print 2>/dev/null | sort | tail -n 1)"
    if [[ -n "${baseline_json}" ]]; then
      run_dir="${baseline_json%/coinjoin-analysis_data/coinjoin_tx_info.json}"
    fi
  fi

  [[ -n "${run_dir}" ]] || return 0
  mkdir -p "${RESULT_DIR}/${ENGINE}"
  [[ -s "${run_dir}/coinjoinPipeline_data/unified_report.json" ]] \
    && cp "${run_dir}/coinjoinPipeline_data/unified_report.json" "${RESULT_DIR}/${ENGINE}/"
  [[ -s "${run_dir}/coinjoinPipeline_data/unified_report.md" ]] \
    && cp "${run_dir}/coinjoinPipeline_data/unified_report.md" "${RESULT_DIR}/${ENGINE}/"
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
  stop_pod_watcher
  if (( status != 0 )); then
    dump_kubernetes_diagnostics
    archive_failure_artifacts
  fi
  if [[ -n "${RESULT_DIR}" ]]; then
    mkdir -p "${RESULT_DIR}/${ENGINE}/pbs-logs"
    find "${WORK_ROOT}" -type f \
      \( -name '*.o[0-9]*' -o -name '*.e[0-9]*' -o -name '*.pbs.log' \) \
      -exec cp -t "${RESULT_DIR}/${ENGINE}/pbs-logs" {} + 2>/dev/null || true
    copy_analysis_artifacts || true
    # The work root is deleted below; copy out everything a post-mortem needs.
    [[ -s "${POD_FAILURE_FILE}" ]] \
      && cp "${POD_FAILURE_FILE}" "${RESULT_DIR}/${ENGINE}/pod-failures.txt" || true
    [[ -s "${PIPELINE_OUTPUT_FILE}" ]] \
      && cp "${PIPELINE_OUTPUT_FILE}" "${RESULT_DIR}/${ENGINE}/pipeline-output.log" || true
    for run_logs in "${LOGS_ROOT}"/*/logs; do
      [[ -d "${run_logs}" ]] || continue
      mkdir -p "${RESULT_DIR}/${ENGINE}/stage-logs"
      cp -r "${run_logs}"/. "${RESULT_DIR}/${ENGINE}/stage-logs/" 2>/dev/null || true
    done
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

if [[ -n "${PBS_BLOCKSCI_LOCAL_IMAGE}" ]]; then
  export_pbs_docker_archive "${PBS_BLOCKSCI_LOCAL_IMAGE}" blocksci --pbs-blocksci-image
fi
if [[ -n "${PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE}" ]]; then
  export_pbs_docker_archive "${PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE}" coinjoin-analysis --pbs-coinjoin-analysis-image
fi

echo "Creating k3d cluster ${CLUSTER_NAME} with direct shared storage ${WORK_ROOT}..."
local_images_build "${ENGINE}" "${PROJECT_DIR}/scenarios/${SCENARIO}"

k3d cluster create "${CLUSTER_NAME}" \
  --servers 1 --agents "${K3D_AGENTS:-2}" --wait --timeout "${K3D_WAIT_TIMEOUT:-240s}" \
  --volume "${WORK_ROOT}:${WORK_ROOT}@all"
k3d kubeconfig get "${CLUSTER_NAME}" >"${HOST_KUBECONFIG}"
local_images_import "${CLUSTER_NAME}"
wait_for_kubernetes_nodes

# Both the Kubernetes API and the NodePort listeners belong to the k3d node
# containers, not to the host's default Docker bridge. k3d publishes only the
# API port, so an emulator manager on the bridge gateway cannot reach the
# btc-node NodePort and its RPC wait times out. Put the manager in the cluster
# network and address the server node directly.
K3D_NETWORK="k3d-${CLUSTER_NAME}"
K3D_SERVER="k3d-${CLUSTER_NAME}-server-0"
K3D_SERVER_IP="$(python3 - "${K3D_SERVER}" "${K3D_NETWORK}" <<'PY'
import json
import subprocess
import sys

server, network = sys.argv[1:]
container = json.loads(subprocess.check_output(["docker", "inspect", server]))[0]
address = (container.get("NetworkSettings", {}).get("Networks", {}).get(network, {}).get("IPAddress", ""))
if not address:
    raise SystemExit(f"FAIL: could not determine {server}'s address on Docker network {network}")
print(address)
PY
)"

cp "${HOST_KUBECONFIG}" "${CONTAINER_KUBECONFIG}"
KUBE_CLUSTER="$(kubectl --kubeconfig "${CONTAINER_KUBECONFIG}" config view --minify -o jsonpath='{.contexts[0].context.cluster}')"
kubectl --kubeconfig "${CONTAINER_KUBECONFIG}" config set-cluster "${KUBE_CLUSTER}" \
  --server="https://${K3D_SERVER_IP}:6443" --insecure-skip-tls-verify=true >/dev/null
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
export EMULATION_LOGS_DIR="${LOGS_ROOT}"
export COINJOIN_EMULATOR_IMAGE
export KUBERNETES_EMULATOR_CONTAINER_NETWORK="${K3D_NETWORK}"
export KUBERNETES_CONTROL_IP="${K3D_SERVER_IP}"
export KUBERNETES_STORAGE_UID="$(id -u)"
export KUBERNETES_STORAGE_GID="$(id -g)"
echo "Emulator manager network: ${K3D_NETWORK}; Kubernetes NodePort host: ${K3D_SERVER_IP}"

echo "Running ${ENGINE} Kubernetes emulation followed by ${PBS_DESCRIPTION} analyzers..."
watch_pod_failures &
POD_WATCHER_PID=$!
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
    --analysis-pbs \
    --blocksci-pbs \
    "${MIN_INPUT_COUNT_ARGS[@]}" \
    "${PBS_IMAGE_ARGS[@]}" \
    "${PARALLEL_ARGS[@]}" \
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
stop_pod_watcher

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

if [[ "${E2E_PARALLEL}" == 1 ]]; then
  if ! grep -q "\[pipeline\] START: Parallel analysis" "${PIPELINE_OUTPUT_FILE}"; then
    echo "FAIL: expected --parallel to open the 'Parallel analysis' pipeline stage" >&2
    exit 1
  fi
  if ! grep -q "\[pipeline\] DONE: Parallel analysis" "${PIPELINE_OUTPUT_FILE}"; then
    echo "FAIL: expected 'Parallel analysis' pipeline stage to complete" >&2
    exit 1
  fi
  # The report is a node of the analysis graph: it may start only after both
  # analyzers have finished.
  report_line="$(grep -n '\[pipeline\] stage unified-report: started' "${PIPELINE_OUTPUT_FILE}" | head -n 1 | cut -d: -f1)"
  for analyzer in coinjoin-analysis blocksci; do
    done_line="$(grep -n "\[pipeline\] stage ${analyzer}: done" "${PIPELINE_OUTPUT_FILE}" | head -n 1 | cut -d: -f1)"
    if [[ -z "${report_line}" || -z "${done_line}" || "${done_line}" -gt "${report_line}" ]]; then
      echo "FAIL: expected the unified report to start after ${analyzer} finished" >&2
      exit 1
    fi
  done
fi

RUN_DIR="$(find "${LOGS_ROOT}" -mindepth 1 -maxdepth 1 -type d \
  -exec test -s '{}/coinjoinPipeline_data/unified_report.json' \; -print | sort | tail -n 1)"
[[ -n "${RUN_DIR}" ]] || { echo "FAIL: no completed report under ${LOGS_ROOT}" >&2; exit 1; }
[[ -s "${BITCOIN_DATADIR}/regtest/blocks/blk00000.dat" ]] || {
  echo "FAIL: Kubernetes did not write the directly mounted Bitcoin datadir" >&2
  exit 1
}

python3 - "${RUN_DIR}" "${EXPECTED_SCENARIO}" "${EXPECTED_COINJOIN_TYPE}" "${PBS_DESCRIPTION}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
expected_scenario, expected_type, pbs = sys.argv[2:]
report = json.loads((run_dir / "coinjoinPipeline_data/unified_report.json").read_text())
baseline = json.loads((run_dir / "coinjoin-analysis_data/coinjoin_tx_info.json").read_text())
run = report.get("run") or {}
summary = report.get("summary") or {}
if run.get("scenario_name") != expected_scenario:
    raise SystemExit(f"FAIL: scenario {run.get('scenario_name')!r} != {expected_scenario!r}")
if run.get("coinjoin_type") != expected_type:
    raise SystemExit(f"FAIL: coinjoin type {run.get('coinjoin_type')!r} != {expected_type!r}")
baseline_coinjoins = baseline.get("coinjoins") or {}
if not baseline_coinjoins:
    raise SystemExit("FAIL: coinjoin-analysis produced no CoinJoin transactions")
if summary.get("coinjoin_analysis_coinjoins") != len(baseline_coinjoins):
    raise SystemExit(
        "FAIL: report baseline count "
        f"{summary.get('coinjoin_analysis_coinjoins')!r} != "
        f"{len(baseline_coinjoins)} CoinJoins in coinjoin_tx_info.json"
    )
if summary.get("blocksci_detected_coinjoins", 0) < 1:
    raise SystemExit("FAIL: BlockSci detected no CoinJoin transactions")
if "blocksci_agreement_rate" not in summary:
    raise SystemExit("FAIL: report has no analyzer agreement metrics")
if expected_type == "joinmarket":
    events = run_dir / "coinjoin_emulator_data/data/joinmarket_round_events.json"
    if not events.is_file():
        raise SystemExit("FAIL: JoinMarket round events are missing")
print(
    f"PASS: {expected_type} via Kubernetes→shared storage→{pbs}; "
    f"baseline={len(baseline_coinjoins)}, blocksci={summary['blocksci_detected_coinjoins']}"
)
PY

if [[ -n "${RESULT_DIR}" ]]; then
  mkdir -p "${RESULT_DIR}/${ENGINE}"
  cp "${RUN_DIR}/coinjoinPipeline_data/unified_report.json" "${RESULT_DIR}/${ENGINE}/" || true
  cp "${RUN_DIR}/coinjoinPipeline_data/unified_report.md" "${RESULT_DIR}/${ENGINE}/" 2>/dev/null || true
  cp "${RUN_DIR}/coinjoin-analysis_data/coinjoin_tx_info.json" "${RESULT_DIR}/${ENGINE}/" 2>/dev/null || true
  find "${RESULT_DIR}" -type d -empty -delete 2>/dev/null || true
fi
