#!/usr/bin/env bash
# End-to-end S3-compatible full-run: Kubernetes emulation uploads artifacts to
# a local MinIO bucket, the frontend waits on S3 markers, and PBS analyzers
# download/upload through the same bucket. MinIO is published on the Docker
# bridge gateway so one endpoint URL works from the host (marker polling),
# from k3d pods (uploader egress), and from the PBS container (s5cmd).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PBS_SUPPORT_ROOT="${PBS_SUPPORT_ROOT:-${SCRIPT_DIR}/support/pbs}"
PBS_HELPER="${PBS_HELPER:-${PBS_SUPPORT_ROOT}/local-pbs.sh}"
PBS_ENV="${PBS_ENV:-${PBS_SUPPORT_ROOT}/pbs-env.sh}"
ENGINE="${1:-wasabi}"

if [[ "${ENGINE}" != "wasabi" ]]; then
  echo "Usage: $0 [wasabi]  (the S3-compatible e2e currently runs the Wasabi scenario only)" >&2
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
STORAGE_BASE="${PBS_TEST_STORAGE_ROOT:-/storage/github-runner}"
[[ -d "${STORAGE_BASE}" && -w "${STORAGE_BASE}" ]] || {
  echo "FAIL: pre-provisioned writable storage is required: ${STORAGE_BASE}" >&2
  exit 2
}
WORK_ROOT="$(mktemp -d "${STORAGE_BASE}/k3d-s3-${ENGINE}-${RUN_TOKEN}.XXXXXX")"
LOGS_ROOT="${WORK_ROOT}/emulation_logs"
CLUSTER_NAME="${CLUSTER_NAME:-cj-s3-${RESOURCE_ID}}"
NAMESPACE="${NAMESPACE:-cj-s3-$$}"
PBS_CONTAINER_NAME="${PBS_CONTAINER_NAME:-pbs-s3-itest-${RESOURCE_ID}}"
MINIO_CONTAINER_NAME="${MINIO_CONTAINER_NAME:-minio-s3-itest-${RESOURCE_ID}}"
HOST_KUBECONFIG="${WORK_ROOT}/kubeconfig-host.yaml"
IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/ondrejman/}"
WRAPPER_IMAGE="${WRAPPER_IMAGE:-ghcr.io/ondrejman/coinjoin-pipeline:latest}"
COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"
MINIO_IMAGE="${MINIO_IMAGE:-minio/minio:latest}"
RESULT_DIR="${TEST_RESULT_DIR:-}"
KEEP_WORK="${KEEP_TEST_WORK:-0}"
KUBERNETES_S3_TIMEOUT="${KUBERNETES_S3_TIMEOUT:-85m}"
KUBERNETES_DIAGNOSTICS_FILE="${WORK_ROOT}/kubernetes-diagnostics.txt"
PIPELINE_OUTPUT_FILE="${WORK_ROOT}/pipeline-output.log"
S3_ENDPOINT_URL=""
WRAPPER_SOURCE_IMAGE="${WRAPPER_IMAGE}"
COINJOIN_EMULATOR_SOURCE_IMAGE="${COINJOIN_EMULATOR_IMAGE}"
K3D_WRAPPER_IMAGE="coinjoin-pipeline-s3-e2e:${RUN_TOKEN}"
K3D_COINJOIN_EMULATOR_IMAGE="coinjoin-emulator-s3-e2e:${RUN_TOKEN}"
# Optional offline mode for analyzer images. Apptainer cannot see Docker's
# local tag store, so local images are exported into the PBS shared workspace.
PBS_BLOCKSCI_LOCAL_IMAGE="${PBS_BLOCKSCI_LOCAL_IMAGE:-}"
PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE="${PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE:-}"
PBS_IMAGE_ARGS=()

SCENARIO="${SCENARIO:-overactive-local.json}"
EXPECTED_SCENARIO="overactive-local"
EXPECTED_COINJOIN_TYPE="wasabi2"
RUN_ID="e2e-$(date -u +%Y%m%dt%H%M%S)-$$"
BUCKET="coinjoin-e2e"
ARTIFACT_URI="s3://${BUCKET}/runs"
S3_PROFILE="coinjoin"
S3_SECRET_NAME="coinjoin-s3-credentials"
MINIO_ROOT_USER="e2e-access-key"
MINIO_ROOT_PASSWORD="e2e-secret-key-${RUN_TOKEN}"
CREDENTIALS_FILE="${WORK_ROOT}/s3-credentials"

s5() {
  s5cmd --credentials-file "${CREDENTIALS_FILE}" --profile "${S3_PROFILE}" \
    --endpoint-url "${S3_ENDPOINT_URL}" "$@"
}

ensure_source_image() {
  local image="$1"
  if docker image inspect "${image}" >/dev/null 2>&1; then
    echo "Using existing source image ${image}."
    return 0
  fi

  echo "Pulling missing source image ${image}..."
  docker pull "${image}"
}

export_pbs_docker_archive() {
  local image="$1" archive_name="$2" image_flag="$3"
  docker image inspect "${image}" >/dev/null 2>&1 || {
    echo "FAIL: local image for PBS docker-archive export not found: ${image}" >&2
    exit 2
  }
  mkdir -p "${WORK_ROOT}/pbs-images"
  echo "Exporting ${image} as ${archive_name}.tar for offline PBS execution..."
  docker save "${image}" -o "${WORK_ROOT}/pbs-images/${archive_name}.tar"
  chmod 0644 "${WORK_ROOT}/pbs-images/${archive_name}.tar"
  PBS_IMAGE_ARGS+=("${image_flag}" "docker-archive:${WORK_ROOT}/pbs-images/${archive_name}.tar")
}

dump_kubernetes_diagnostics() {
  {
    echo "Kubernetes S3 workflow failed; collecting diagnostics for namespace ${NAMESPACE}..."
    if [[ ! -s "${HOST_KUBECONFIG}" ]]; then
      echo "Kubeconfig is unavailable: ${HOST_KUBECONFIG}"
    else
      kubectl --kubeconfig "${HOST_KUBECONFIG}" get jobs,pods -n "${NAMESPACE}" -o wide || true
      echo "===== namespace events ====="
      kubectl --kubeconfig "${HOST_KUBECONFIG}" get events -n "${NAMESPACE}" \
        --sort-by=.metadata.creationTimestamp || true
      local pod
      while IFS= read -r pod; do
        [[ -n "${pod}" ]] || continue
        echo "===== description: ${pod} ====="
        kubectl --kubeconfig "${HOST_KUBECONFIG}" describe -n "${NAMESPACE}" "${pod}" || true
        echo "===== final 200 log lines: ${pod} ====="
        kubectl --kubeconfig "${HOST_KUBECONFIG}" logs -n "${NAMESPACE}" \
          "${pod}" --all-containers --tail=200 --timestamps || true
      done < <(kubectl --kubeconfig "${HOST_KUBECONFIG}" get pods -n "${NAMESPACE}" \
        -o name 2>/dev/null || true)
    fi
    echo "===== bucket contents ====="
    s5 ls "${ARTIFACT_URI}/${RUN_ID}/*" || true
    echo "===== PBS job history ====="
    qstat -x 2>/dev/null || true
  } >"${KUBERNETES_DIAGNOSTICS_FILE}" 2>&1
  cat "${KUBERNETES_DIAGNOSTICS_FILE}" >&2
}

cleanup() {
  local status=$?
  trap - EXIT
  if (( status != 0 )); then
    dump_kubernetes_diagnostics || true
  fi
  if [[ -n "${RESULT_DIR}" ]]; then
    mkdir -p "${RESULT_DIR}/${ENGINE}"
    for artifact in unified_report.json coinjoin_tx_info.json; do
      [[ -s "${WORK_ROOT}/results/${artifact}" ]] \
        && cp "${WORK_ROOT}/results/${artifact}" "${RESULT_DIR}/${ENGINE}/"
    done
    [[ -s "${KUBERNETES_DIAGNOSTICS_FILE}" ]] \
      && cp "${KUBERNETES_DIAGNOSTICS_FILE}" "${RESULT_DIR}/${ENGINE}/"
    [[ -s "${PIPELINE_OUTPUT_FILE}" ]] \
      && cp "${PIPELINE_OUTPUT_FILE}" "${RESULT_DIR}/${ENGINE}/"
  fi
  docker rm -f "${PBS_CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker rm -f "${MINIO_CONTAINER_NAME}" >/dev/null 2>&1 || true
  if [[ "${KEEP_CLUSTER:-0}" != 1 ]]; then
    k3d cluster delete "${CLUSTER_NAME}" >/dev/null 2>&1 || true
  fi
  docker image rm "${K3D_WRAPPER_IMAGE}" "${K3D_COINJOIN_EMULATOR_IMAGE}" \
    >/dev/null 2>&1 || true
  if [[ "${KEEP_WORK}" != 1 ]]; then
    rm -rf "${WORK_ROOT}"
  else
    echo "Keeping test work directory: ${WORK_ROOT}" >&2
  fi
  exit "${status}"
}
trap cleanup EXIT

# Recover from an interrupted previous run without touching unrelated resources.
k3d cluster delete "${CLUSTER_NAME}" >/dev/null 2>&1 || true
docker rm -f "${PBS_CONTAINER_NAME}" "${MINIO_CONTAINER_NAME}" >/dev/null 2>&1 || true

mkdir -p "${LOGS_ROOT}" "${WORK_ROOT}/bin" "${WORK_ROOT}/results"
chmod 0777 "${WORK_ROOT}" "${LOGS_ROOT}"

ensure_source_image "${WRAPPER_SOURCE_IMAGE}"
ensure_source_image "${COINJOIN_EMULATOR_SOURCE_IMAGE}"
docker tag "${WRAPPER_SOURCE_IMAGE}" "${K3D_WRAPPER_IMAGE}"
docker tag "${COINJOIN_EMULATOR_SOURCE_IMAGE}" "${K3D_COINJOIN_EMULATOR_IMAGE}"

if [[ -n "${PBS_BLOCKSCI_LOCAL_IMAGE}" ]]; then
  export_pbs_docker_archive "${PBS_BLOCKSCI_LOCAL_IMAGE}" blocksci --pbs-blocksci-image
fi
if [[ -n "${PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE}" ]]; then
  export_pbs_docker_archive \
    "${PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE}" coinjoin-analysis --pbs-coinjoin-analysis-image
fi

echo "Extracting s5cmd from ${WRAPPER_SOURCE_IMAGE} for the host and the PBS container..."
S5CMD_SOURCE_CONTAINER="$(docker create "${WRAPPER_SOURCE_IMAGE}")"
docker cp "${S5CMD_SOURCE_CONTAINER}:/usr/local/bin/s5cmd" "${WORK_ROOT}/bin/s5cmd"
docker rm -f "${S5CMD_SOURCE_CONTAINER}" >/dev/null
chmod 0755 "${WORK_ROOT}/bin/s5cmd"
export PATH="${WORK_ROOT}/bin:${PATH}"

GATEWAY="${CONTAINER_KUBE_HOST:-$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')}"
echo "Starting MinIO ${MINIO_CONTAINER_NAME} published on the bridge gateway ${GATEWAY}..."
docker run -d --name "${MINIO_CONTAINER_NAME}" \
  -e MINIO_ROOT_USER="${MINIO_ROOT_USER}" \
  -e MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD}" \
  -p 9000 "${MINIO_IMAGE}" server /data >/dev/null
MINIO_PORT="$(docker port "${MINIO_CONTAINER_NAME}" 9000/tcp | head -n 1 | awk -F: '{print $NF}')"
S3_ENDPOINT_URL="http://${GATEWAY}:${MINIO_PORT}"

# 0644: the PBS jobs run as pbsuser inside the rig container; the credentials
# are throwaway test values scoped to this MinIO instance.
umask 022
printf '[%s]\naws_access_key_id = %s\naws_secret_access_key = %s\n' \
  "${S3_PROFILE}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" >"${CREDENTIALS_FILE}"

echo "Waiting for MinIO readiness at ${S3_ENDPOINT_URL}..."
for _ in $(seq 1 60); do
  if s5 ls >/dev/null 2>&1; then break; fi
  sleep 2
done
s5 ls >/dev/null || { echo "FAIL: MinIO did not become ready" >&2; exit 1; }
s5 mb "s3://${BUCKET}" >/dev/null

echo "Creating k3d cluster ${CLUSTER_NAME} (no shared storage needed in S3 mode)..."
k3d cluster create "${CLUSTER_NAME}" \
  --servers 1 --agents "${K3D_AGENTS:-2}" --wait --timeout "${K3D_WAIT_TIMEOUT:-240s}"
echo "Importing wrapper and emulator images into ${CLUSTER_NAME}..."
k3d image import --cluster "${CLUSTER_NAME}" \
  "${K3D_WRAPPER_IMAGE}" "${K3D_COINJOIN_EMULATOR_IMAGE}"
WRAPPER_IMAGE="${K3D_WRAPPER_IMAGE}"
COINJOIN_EMULATOR_IMAGE="${K3D_COINJOIN_EMULATOR_IMAGE}"
k3d kubeconfig get "${CLUSTER_NAME}" >"${HOST_KUBECONFIG}"
kubectl --kubeconfig "${HOST_KUBECONFIG}" wait node --all --for=condition=Ready --timeout=240s

kubectl --kubeconfig "${HOST_KUBECONFIG}" create namespace "${NAMESPACE}"
kubectl --kubeconfig "${HOST_KUBECONFIG}" -n "${NAMESPACE}" create secret generic "${S3_SECRET_NAME}" \
  --from-literal=S3_ACCESS_KEY_ID="${MINIO_ROOT_USER}" \
  --from-literal=S3_SECRET_ACCESS_KEY="${MINIO_ROOT_PASSWORD}" \
  --from-literal=S3_DEFAULT_REGION='us-east-1'

export PBS_CONTAINER_NAME PBS_WORKDIR_HOST="${WORK_ROOT}" PBS_WORKDIR_CONTAINER="${WORK_ROOT}"
"${PBS_HELPER}" start
source "${PBS_ENV}"
# OpenPBS jobs run with a minimal PATH that includes /usr/bin but not
# necessarily /usr/local/bin, even though the container's interactive shell
# sees both. Install the local test binary where compute jobs can resolve it.
docker cp "${WORK_ROOT}/bin/s5cmd" "${PBS_CONTAINER_NAME}:/usr/bin/s5cmd"
docker exec -u root "${PBS_CONTAINER_NAME}" chmod 0755 /usr/bin/s5cmd

export PBS_CLIENT_WORKDIR="${WORK_ROOT}"
export PBS_FRONTEND_DIRECT=1
export EMULATION_LOGS_DIR="${LOGS_ROOT}"
export WRAPPER_IMAGE COINJOIN_EMULATOR_IMAGE

echo "Running the S3-compatible full-run for run ${RUN_ID}..."
set +e
(
  cd "${PROJECT_DIR}"
  timeout --foreground "${KUBERNETES_S3_TIMEOUT}" ./runIt.sh full-run \
    --engine "${ENGINE}" \
    --scenario "${SCENARIO}" \
    --driver kubernetes \
    --namespace "${NAMESPACE}" \
    --reuse-namespace \
    --kubeconfig "${HOST_KUBECONFIG}" \
    --image-prefix "${IMAGE_PREFIX}" \
    --artifact-backend s3 \
    --artifact-uri "${ARTIFACT_URI}" \
    --run-id "${RUN_ID}" \
    --s3-endpoint-url "${S3_ENDPOINT_URL}" \
    --s3-secret-name "${S3_SECRET_NAME}" \
    --s3-credentials-file "${CREDENTIALS_FILE}" \
    --s3-profile "${S3_PROFILE}" \
    --analysisPbs \
    --blocksciPbs \
    --test-values \
    --min-input-count 15 \
    "${PBS_IMAGE_ARGS[@]}" \
    --pbs-ncpus 2 \
    --pbs-mem 4gb \
    --pbs-scratch 2gb \
    --pbs-walltime 00:30:00 \
    --emulation-timeout 3600
) 2>&1 | tee "${PIPELINE_OUTPUT_FILE}"
PIPELINE_STATUS=${PIPESTATUS[0]}
set -e
(( PIPELINE_STATUS == 0 )) || exit "${PIPELINE_STATUS}"

echo "Verifying completion markers and results in the bucket..."
for marker in .k8s/upload.done .pbs/coinjoin-analysis.done .pbs/blocksci.done .pbs/unified-report.done; do
  s5 ls "${ARTIFACT_URI}/${RUN_ID}/${marker}" >/dev/null || {
    echo "FAIL: missing completion marker ${marker}" >&2
    exit 1
  }
done
for marker in .k8s/upload.failed .pbs/coinjoin-analysis.failed .pbs/blocksci.failed .pbs/unified-report.failed; do
  if s5 ls "${ARTIFACT_URI}/${RUN_ID}/${marker}" >/dev/null 2>&1; then
    echo "FAIL: failure marker present: ${marker}" >&2
    exit 1
  fi
done

s5 cp "${ARTIFACT_URI}/${RUN_ID}/coinjoinPipeline_data/unified_report.json" \
  "${WORK_ROOT}/results/unified_report.json" >/dev/null
s5 cp "${ARTIFACT_URI}/${RUN_ID}/coinjoin-analysis_data/coinjoin_tx_info.json" \
  "${WORK_ROOT}/results/coinjoin_tx_info.json" >/dev/null

python3 - "${WORK_ROOT}/results" "${EXPECTED_SCENARIO}" "${EXPECTED_COINJOIN_TYPE}" <<'PY'
import json
import sys
from pathlib import Path

results = Path(sys.argv[1])
expected_scenario, expected_type = sys.argv[2:]
report = json.loads((results / "unified_report.json").read_text())
baseline = json.loads((results / "coinjoin_tx_info.json").read_text())
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
print(
    f"PASS: {expected_type} via Kubernetes→S3 (MinIO)→PBS full-run; "
    f"baseline={len(baseline)}, blocksci={summary['blocksci_detected_coinjoins']}"
)
PY
