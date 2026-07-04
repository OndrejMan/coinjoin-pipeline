#!/usr/bin/env bash
set -euo pipefail

# Happy-path e2e: full-run --parallel on the local Docker runtime.
# Both the coinjoin-analysis and BlockSci analyzers must be scheduled
# concurrently under the wrapper's "Parallel analysis" pipeline stage,
# and the unified report export must still produce the expected artifacts.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROJECT_DIR}/.." && pwd)"
LOGS_DIR="${EMULATION_LOGS_DIR:-${PROJECT_DIR}/emulation_logs}"
BEFORE_FILE="$(mktemp)"
AFTER_FILE="$(mktemp)"
RUN_LOG="$(mktemp)"
RUN_PID=""
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-3600}"
IMAGE_MODE="${1:-upstream}"

if [[ $# -gt 1 || ( "${IMAGE_MODE}" != "upstream" && "${IMAGE_MODE}" != "local" ) ]]; then
  echo "Usage: $0 [local]" >&2
  echo "Pass no argument to use upstream images, or 'local' to build and use local images only." >&2
  exit 2
fi

LOCAL_TAG="${LOCAL_TAG:-bitcoinanalysis-local}"
LOCAL_WRAPPER_IMAGE="${LOCAL_WRAPPER_IMAGE:-blocksciemulatoranalysis:${LOCAL_TAG}}"
LOCAL_BLOCKSCI_IMAGE="${LOCAL_BLOCKSCI_IMAGE:-blocksci-complete:${LOCAL_TAG}}"
LOCAL_EMULATOR_IMAGE="${LOCAL_EMULATOR_IMAGE:-coinjoin-emulator:${LOCAL_TAG}}"
LOCAL_COINJOIN_ANALYSIS_IMAGE="${LOCAL_COINJOIN_ANALYSIS_IMAGE:-coinjoin-analysis:${LOCAL_TAG}}"
WRAPPER_SOURCE_DIR="${WRAPPER_SOURCE_DIR:-${REPO_ROOT}/blocksciEmulatorAnalysis}"
BLOCKSCI_SOURCE_DIR="${BLOCKSCI_SOURCE_DIR:-${REPO_ROOT}/blocksci}"
EMULATOR_SOURCE_DIR="${EMULATOR_SOURCE_DIR:-${REPO_ROOT}/coinjoin-emulator}"
COINJOIN_ANALYSIS_SOURCE_DIR="${COINJOIN_ANALYSIS_SOURCE_DIR:-${REPO_ROOT}/coinjoin-analysis}"

cleanup() {
  if [[ -n "${RUN_PID}" ]] && kill -0 "${RUN_PID}" >/dev/null 2>&1; then
    docker stop blocksci_analyzer >/dev/null 2>&1 || true
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
  echo "Building local BlockSci image ${LOCAL_BLOCKSCI_IMAGE} from ${BLOCKSCI_SOURCE_DIR}..."
  docker build -t "${LOCAL_BLOCKSCI_IMAGE}" "${BLOCKSCI_SOURCE_DIR}"

  echo "Building local CoinJoin emulator image ${LOCAL_EMULATOR_IMAGE} from ${EMULATOR_SOURCE_DIR}..."
  docker build -t "${LOCAL_EMULATOR_IMAGE}" "${EMULATOR_SOURCE_DIR}"

  echo "Building local wrapper image ${LOCAL_WRAPPER_IMAGE} from ${WRAPPER_SOURCE_DIR}..."
  docker build -t "${LOCAL_WRAPPER_IMAGE}" -f "${WRAPPER_SOURCE_DIR}/client/Dockerfile" "${WRAPPER_SOURCE_DIR}"

  echo "Building local coinjoin-analysis image ${LOCAL_COINJOIN_ANALYSIS_IMAGE} from ${COINJOIN_ANALYSIS_SOURCE_DIR}..."
  docker build -t "${LOCAL_COINJOIN_ANALYSIS_IMAGE}" -f "${COINJOIN_ANALYSIS_SOURCE_DIR}/docker/analysis.Dockerfile" "${COINJOIN_ANALYSIS_SOURCE_DIR}"

  WRAPPER_IMAGE="${LOCAL_WRAPPER_IMAGE}"
  BLOCKSCI_IMAGE="${LOCAL_BLOCKSCI_IMAGE}"
  COINJOIN_EMULATOR_IMAGE="${LOCAL_EMULATOR_IMAGE}"
  COINJOIN_ANALYSIS_IMAGE="${LOCAL_COINJOIN_ANALYSIS_IMAGE}"
  BLOCKSCI_PULL_POLICY=never
  COINJOIN_EMULATOR_PULL_POLICY=never
  COINJOIN_ANALYSIS_PULL_POLICY=never
  COINJOIN_EMULATOR_IMAGE_PREFIX=""
  COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD=1
else
  WRAPPER_IMAGE="${WRAPPER_IMAGE:-ghcr.io/ondrejman/blocksciemulatoranalysis:latest}"
  BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-ghcr.io/ondrejman/blocksci-complete:latest}"
  COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"
  COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}"
  BLOCKSCI_PULL_POLICY="${BLOCKSCI_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY:-always}"
  COINJOIN_ANALYSIS_PULL_POLICY="${COINJOIN_ANALYSIS_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_IMAGE_PREFIX="${COINJOIN_EMULATOR_IMAGE_PREFIX:-ghcr.io/ondrejman/}"
  COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:-}"
fi

echo "Running real Docker --parallel workflow in ${IMAGE_MODE} image mode with logs in ${LOGS_DIR}..."
echo "Using wrapper image ${WRAPPER_IMAGE}, BlockSci image ${BLOCKSCI_IMAGE}, emulator image ${COINJOIN_EMULATOR_IMAGE}, and analyzer image ${COINJOIN_ANALYSIS_IMAGE}."
(
  (
    cd "${PROJECT_DIR}"
    EMULATION_LOGS_DIR="${LOGS_DIR}" \
    WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
    BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
    BLOCKSCI_PULL_POLICY="${BLOCKSCI_PULL_POLICY}" \
    COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
    COINJOIN_ANALYSIS_PULL_POLICY="${COINJOIN_ANALYSIS_PULL_POLICY}" \
    COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
    COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY}" \
    COINJOIN_EMULATOR_IMAGE_PREFIX="${COINJOIN_EMULATOR_IMAGE_PREFIX}" \
    COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD}" \
    bash runIt.sh --engine wasabi --scenario scenarios/overactive-local.json --parallel
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
    docker stop blocksci_analyzer >/dev/null 2>&1 || true
    wait "${RUN_PID}" >/dev/null 2>&1 || true
    RUN_PID=""
    exit 1
  fi

  sleep 1
done

if [[ "${run_finished}" == "false" ]] && kill -0 "${RUN_PID}" >/dev/null 2>&1; then
  echo "FAIL: runIt.sh did not exit within ${RUN_TIMEOUT_SECONDS}s" >&2
  docker stop blocksci_analyzer >/dev/null 2>&1 || true
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

# --parallel must drive both analyzers through the shared "Parallel analysis"
# stage instead of the serial per-stage scheduling.
if ! grep -q "\[pipeline\] START: Parallel analysis" "${RUN_LOG}"; then
  echo "FAIL: expected --parallel to open the 'Parallel analysis' pipeline stage" >&2
  echo "Observed log tail:" >&2
  tail -n 80 "${RUN_LOG}" >&2 || true
  exit 1
fi
if ! grep -q "\[pipeline\] DONE: Parallel analysis" "${RUN_LOG}"; then
  echo "FAIL: expected 'Parallel analysis' pipeline stage to complete" >&2
  exit 1
fi
if ! grep -q "\[pipeline\] START: Unified report export" "${RUN_LOG}"; then
  echo "FAIL: expected parallel run to follow with the unified report export stage" >&2
  exit 1
fi

list_run_dirs >"${AFTER_FILE}"

RUN_DIR="$(LC_ALL=C comm -13 "${BEFORE_FILE}" "${AFTER_FILE}" | tail -n 1)"
if [[ -z "${RUN_DIR}" ]]; then
  RUN_DIR="$(find "${LOGS_DIR}" -mindepth 1 -maxdepth 1 -type d -name '*overactive-local*' \
    -exec test -s "{}/coinjoin_emulator_data/scenario.json" \; -print | LC_ALL=C sort | tail -n 1)"
fi

if [[ -z "${RUN_DIR}" || ! -d "${RUN_DIR}" ]]; then
  echo "FAIL: no overactive-local run directory was created under ${LOGS_DIR}" >&2
  exit 1
fi

for artifact in \
  coinjoin_emulator_data/scenario.json \
  coinjoin-analysis_data/coinjoin_tx_info.json \
  blocksciEmulatorAnalysis_data/unified_report.json \
  blocksciEmulatorAnalysis_data/unified_report.md \
  blocksci_data/config.json; do
  if [[ ! -s "${RUN_DIR}/${artifact}" ]]; then
    echo "FAIL: expected artifact missing or empty: ${RUN_DIR}/${artifact}" >&2
    exit 1
  fi
done

python3 - "${RUN_DIR}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
scenario = json.loads((run_dir / "coinjoin_emulator_data" / "scenario.json").read_text(encoding="utf-8"))
report = json.loads((run_dir / "blocksciEmulatorAnalysis_data" / "unified_report.json").read_text(encoding="utf-8"))
baseline = json.loads((run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json").read_text(encoding="utf-8"))

if scenario.get("name") != "overactive-local":
    raise SystemExit(f"FAIL: expected scenario name overactive-local, got {scenario.get('name')!r}")

run = report.get("run") or {}
if run.get("scenario_name") != "overactive-local":
    raise SystemExit(f"FAIL: expected report scenario_name overactive-local, got {run.get('scenario_name')!r}")
if run.get("coinjoin_type") != "wasabi2":
    raise SystemExit(f"FAIL: expected report coinjoin_type wasabi2, got {run.get('coinjoin_type')!r}")

summary = report.get("summary") or {}
if not baseline:
    raise SystemExit("FAIL: coinjoin-analysis produced no records")
if summary.get("blocksci_detected_coinjoins", 0) < 1:
    raise SystemExit("FAIL: BlockSci detected no CoinJoin transactions")
if "blocksci_agreement_rate" not in summary:
    raise SystemExit("FAIL: unified report has no analyzer agreement metrics")
PY

echo "PASS: real Docker --parallel runIt workflow completed and produced artifacts in ${RUN_DIR}."
