#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROJECT_DIR}/.." && pwd)"
LOGS_DIR="${EMULATION_LOGS_DIR:-${PROJECT_DIR}/emulation_logs}"
BEFORE_FILE="$(mktemp)"
AFTER_FILE="$(mktemp)"
RUN_LOG="$(mktemp)"
RUN_PID=""
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-14400}"
IMAGE_MODE="${1:-upstream}"

if [[ $# -gt 1 || ( "${IMAGE_MODE}" != "upstream" && "${IMAGE_MODE}" != "local" ) ]]; then
  echo "Usage: $0 [local]" >&2
  echo "Pass no argument to use upstream images, or 'local' to build and use local images only." >&2
  exit 2
fi

LOCAL_TAG="${LOCAL_TAG:-coinjoin-pipeline-local}"
LOCAL_BLOCKSCI_BASE_IMAGE="${LOCAL_BLOCKSCI_BASE_IMAGE:-blocksci-cj:${LOCAL_TAG}}"
LOCAL_BLOCKSCI_IMAGE="${LOCAL_BLOCKSCI_IMAGE:-blocksci-complete:${LOCAL_TAG}}"
LOCAL_EMULATOR_IMAGE="${LOCAL_EMULATOR_IMAGE:-coinjoin-emulator:${LOCAL_TAG}}"
LOCAL_COINJOIN_ANALYSIS_IMAGE="${LOCAL_COINJOIN_ANALYSIS_IMAGE:-coinjoin-analysis:${LOCAL_TAG}}"
BLOCKSCI_SOURCE_DIR="${BLOCKSCI_SOURCE_DIR:-${REPO_ROOT}/blocksci}"
EMULATOR_SOURCE_DIR="${EMULATOR_SOURCE_DIR:-${REPO_ROOT}/coinjoin-emulator}"
COINJOIN_ANALYSIS_SOURCE_DIR="${COINJOIN_ANALYSIS_SOURCE_DIR:-${REPO_ROOT}/coinjoin-analysis}"
LOCAL_IMAGES_PREBUILT="${LOCAL_IMAGES_PREBUILT:-0}"
# Wrappers such as test-runIt-liquidity-local-docker.sh reuse this workflow
# with another Wasabi scenario and extra liquidity checks.
E2E_SCENARIO_FILE="${E2E_SCENARIO_FILE:-scenarios/overactive-local.json}"
E2E_SCENARIO_NAME="${E2E_SCENARIO_NAME:-overactive-local}"
read -r -a E2E_LIQUIDITY_CHECKS <<<"${E2E_LIQUIDITY_CHECKS:-}"
# delete.sh reaps every cjp-*-<pid> project whose pid is gone, so the suffix
# must be this script's pid, alive for the whole run.
COMPOSE_PROJECT="cjp-${E2E_SCENARIO_NAME}-$$"

stop_blocksci() {
  docker compose -f "${PROJECT_DIR}/pipeline/compose.yaml" -p "${COMPOSE_PROJECT}" \
    stop blocksci >/dev/null 2>&1 || true
}

cleanup() {
  if [[ -n "${RUN_PID}" ]] && kill -0 "${RUN_PID}" >/dev/null 2>&1; then
    stop_blocksci
    wait "${RUN_PID}" >/dev/null 2>&1 || true
  fi
  rm -f "${BEFORE_FILE}" "${AFTER_FILE}" "${RUN_LOG}"
}
trap cleanup EXIT

list_run_dirs() {
  find "${LOGS_DIR}" -mindepth 1 -maxdepth 1 -type d \
    -exec test -s "{}/coinjoin_emulator_data/scenario.json" \; -print |
    LC_ALL=C sort
}

if ! command -v docker >/dev/null 2>&1; then
  echo "FAIL: docker command not found" >&2
  exit 2
fi

if ! docker info >/dev/null 2>&1; then
  echo "FAIL: docker daemon is not reachable" >&2
  exit 2
fi

mkdir -p "${LOGS_DIR}"
list_run_dirs >"${BEFORE_FILE}"

if [[ "${IMAGE_MODE}" == "local" ]]; then
  if [[ "${LOCAL_IMAGES_PREBUILT}" != "1" ]]; then
    echo "Building local BlockSci base image ${LOCAL_BLOCKSCI_BASE_IMAGE} from ${BLOCKSCI_SOURCE_DIR}..."
    docker build -t "${LOCAL_BLOCKSCI_BASE_IMAGE}" -f "${BLOCKSCI_SOURCE_DIR}/Dockerfile" "${BLOCKSCI_SOURCE_DIR}"

    echo "Building local BlockSci complete image ${LOCAL_BLOCKSCI_IMAGE} from ${BLOCKSCI_SOURCE_DIR}..."
    docker build \
      --build-arg "BLOCKSCI_BASE_IMAGE=${LOCAL_BLOCKSCI_BASE_IMAGE}" \
      -t "${LOCAL_BLOCKSCI_IMAGE}" \
      -f "${BLOCKSCI_SOURCE_DIR}/Dockerfile_complete" \
      "${BLOCKSCI_SOURCE_DIR}"

    echo "Building local CoinJoin emulator image ${LOCAL_EMULATOR_IMAGE} from ${EMULATOR_SOURCE_DIR}..."
    docker build -t "${LOCAL_EMULATOR_IMAGE}" "${EMULATOR_SOURCE_DIR}"


    echo "Building local coinjoin-analysis image ${LOCAL_COINJOIN_ANALYSIS_IMAGE} from ${COINJOIN_ANALYSIS_SOURCE_DIR}..."
    docker build -t "${LOCAL_COINJOIN_ANALYSIS_IMAGE}" -f "${COINJOIN_ANALYSIS_SOURCE_DIR}/docker/analysis.Dockerfile" "${COINJOIN_ANALYSIS_SOURCE_DIR}"
  fi

  BLOCKSCI_IMAGE="${LOCAL_BLOCKSCI_IMAGE}"
  COINJOIN_EMULATOR_IMAGE="${LOCAL_EMULATOR_IMAGE}"
  COINJOIN_ANALYSIS_IMAGE="${LOCAL_COINJOIN_ANALYSIS_IMAGE}"
  BLOCKSCI_PULL_POLICY=never
  COINJOIN_EMULATOR_PULL_POLICY=never
  COINJOIN_ANALYSIS_PULL_POLICY=never
  COINJOIN_EMULATOR_IMAGE_PREFIX=""
  COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD=1
else
  BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-ghcr.io/ondrejman/blocksci-complete:latest}"
  COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/emulator-manager:latest}"
  COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}"
  BLOCKSCI_PULL_POLICY="${BLOCKSCI_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY:-always}"
  COINJOIN_ANALYSIS_PULL_POLICY="${COINJOIN_ANALYSIS_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_IMAGE_PREFIX="${COINJOIN_EMULATOR_IMAGE_PREFIX:-ghcr.io/ondrejman/}"
  COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:-}"
fi

if [[ "${IMAGE_MODE}" == "local" ]]; then
  echo "Verifying BlockSci runtime in ${BLOCKSCI_IMAGE}..."
  docker run --rm --entrypoint /bin/bash "${BLOCKSCI_IMAGE}" -lc \
    'command -v blocksci_parser >/dev/null && python3 -c "import blocksci"'
fi

echo "Running real Docker workflow in ${IMAGE_MODE} image mode with logs in ${LOGS_DIR}..."
echo "Using BlockSci image ${BLOCKSCI_IMAGE}, emulator image ${COINJOIN_EMULATOR_IMAGE}, and analyzer image ${COINJOIN_ANALYSIS_IMAGE}."
(
  (
    cd "${PROJECT_DIR}"
    EMULATION_LOGS_DIR="${LOGS_DIR}" \
    BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
    BLOCKSCI_PULL_POLICY="${BLOCKSCI_PULL_POLICY}" \
    COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
    COINJOIN_ANALYSIS_PULL_POLICY="${COINJOIN_ANALYSIS_PULL_POLICY}" \
    COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
    COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY}" \
    COINJOIN_EMULATOR_IMAGE_PREFIX="${COINJOIN_EMULATOR_IMAGE_PREFIX}" \
    COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD}" \
    COINJOIN_COMPOSE_PROJECT="${COMPOSE_PROJECT}" \
    bash runIt.sh --engine wasabi --scenario "${E2E_SCENARIO_FILE}" \
      --min-input-count 15
  ) 2>&1 | tee "${RUN_LOG}"
) &
RUN_PID=$!

run_finished=false
for ((elapsed = 0; elapsed < RUN_TIMEOUT_SECONDS; elapsed++)); do
  if ! kill -0 "${RUN_PID}" >/dev/null 2>&1; then
    run_finished=true
    break
  fi

  if grep -q "Use Control-C to stop this server" "${RUN_LOG}" ||
    grep -q "http://127.0.0.1:8888/tree?token" "${RUN_LOG}"; then
    echo "FAIL: noninteractive runIt.sh launched the BlockSci Jupyter server" >&2
    echo "Expected BLOCKSCI_LAUNCH_JUPYTER=0 to make analysis exit after exports." >&2
    stop_blocksci
    wait "${RUN_PID}" >/dev/null 2>&1 || true
    RUN_PID=""
    exit 1
  fi

  sleep 1
done

if [[ "${run_finished}" == "false" ]] && kill -0 "${RUN_PID}" >/dev/null 2>&1; then
  echo "FAIL: runIt.sh did not exit within ${RUN_TIMEOUT_SECONDS}s" >&2
  stop_blocksci
  wait "${RUN_PID}" >/dev/null 2>&1 || true
  RUN_PID=""
  exit 1
fi

set +e
wait "${RUN_PID}"
RUN_EXIT_CODE=$?
set -e
RUN_PID=""

if [[ "${RUN_EXIT_CODE}" -ne 0 ]]; then
  echo "FAIL: runIt.sh exited with code ${RUN_EXIT_CODE}" >&2
  exit "${RUN_EXIT_CODE}"
fi

if ! grep -q "Parsing complete. Skipping interactive BlockSci environment." "${RUN_LOG}"; then
  echo "FAIL: expected analysis to skip the interactive BlockSci environment" >&2
  exit 1
fi

list_run_dirs >"${AFTER_FILE}"

RUN_DIR="$(LC_ALL=C comm -13 "${BEFORE_FILE}" "${AFTER_FILE}" | tail -n 1)"
if [[ -z "${RUN_DIR}" ]]; then
  RUN_DIR="$(find "${LOGS_DIR}" -mindepth 1 -maxdepth 1 -type d -name "*${E2E_SCENARIO_NAME}*" \
    -exec test -s "{}/coinjoin_emulator_data/scenario.json" \; -print | LC_ALL=C sort | tail -n 1)"
fi

if [[ -z "${RUN_DIR}" || ! -d "${RUN_DIR}" ]]; then
  echo "FAIL: no ${E2E_SCENARIO_NAME} run directory was created under ${LOGS_DIR}" >&2
  exit 1
fi

for artifact in \
  coinjoin_emulator_data/scenario.json \
  coinjoin-analysis_data/coinjoin_tx_info.json \
  coinjoinPipeline_data/unified_report.json \
  coinjoinPipeline_data/unified_report.md \
  blocksci_data/config.json; do
  if [[ ! -s "${RUN_DIR}/${artifact}" ]]; then
    echo "FAIL: expected artifact missing or empty: ${RUN_DIR}/${artifact}" >&2
    exit 1
  fi
done

python3 - "${RUN_DIR}" "${E2E_SCENARIO_NAME}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
expected = sys.argv[2]
scenario = json.loads((run_dir / "coinjoin_emulator_data" / "scenario.json").read_text(encoding="utf-8"))
report = json.loads((run_dir / "coinjoinPipeline_data" / "unified_report.json").read_text(encoding="utf-8"))

if scenario.get("name") != expected:
    raise SystemExit(f"FAIL: expected scenario name {expected}, got {scenario.get('name')!r}")

run = report.get("run") or {}
if run.get("scenario_name") != expected:
    raise SystemExit(f"FAIL: expected report scenario_name {expected}, got {run.get('scenario_name')!r}")
PY

# Local images are built from the checked-out analyzers, which classify every
# liquidity event as the ground truth does; published images may lag behind.
LIQUIDITY_CHECKS=("${E2E_LIQUIDITY_CHECKS[@]}")
if [[ "${IMAGE_MODE}" == "local" ]]; then
  LIQUIDITY_CHECKS+=(--require-blocksci --exact-baseline)
fi
python3 "${SCRIPT_DIR}/support/check_liquidity.py" "${RUN_DIR}/coinjoinPipeline_data" "${LIQUIDITY_CHECKS[@]}"

echo "PASS: real Docker runIt workflow completed and produced artifacts in ${RUN_DIR}."
