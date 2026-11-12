#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=tests/support/local-runit.sh
source "${SCRIPT_DIR}/support/local-runit.sh"
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
# test-runIt-parallel-local-docker.sh sets E2E_PARALLEL=1: the run adds
# --parallel, so both analyzers must run under the "Parallel analysis" stage.
E2E_PARALLEL="${E2E_PARALLEL:-0}"
PARALLEL_ARGS=()
WORKFLOW="Docker"
if [[ "${E2E_PARALLEL}" == 1 ]]; then
  PARALLEL_ARGS=(--parallel)
  WORKFLOW="Docker --parallel"
fi
# delete.sh reaps every cjp-*-<pid> project whose pid is gone, so the suffix
# must be this script's pid, alive for the whole run.
COMPOSE_PROJECT="cjp-${E2E_SCENARIO_NAME}-$$"

cleanup() {
  if [[ -n "${RUN_PID}" ]] && kill -0 "${RUN_PID}" >/dev/null 2>&1; then
    stop_blocksci
    wait "${RUN_PID}" >/dev/null 2>&1 || true
  fi
  rm -f "${BEFORE_FILE}" "${AFTER_FILE}" "${RUN_LOG}"
}
trap cleanup EXIT

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
    build_local_images docker
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

echo "Running real ${WORKFLOW} workflow in ${IMAGE_MODE} image mode with logs in ${LOGS_DIR}..."
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
      --min-input-count 15 "${PARALLEL_ARGS[@]}"
  ) 2>&1 | tee "${RUN_LOG}"
) &
RUN_PID=$!

wait_for_runit runIt.sh stop_blocksci

if ! grep -q "Parsing complete. Skipping interactive BlockSci environment." "${RUN_LOG}"; then
  echo "FAIL: expected analysis to skip the interactive BlockSci environment" >&2
  exit 1
fi

if [[ "${E2E_PARALLEL}" == 1 ]]; then
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
  # The report is a node of the analysis graph: it may start only after both
  # analyzers have finished.
  report_line="$(grep -n '\[pipeline\] stage unified-report: started' "${RUN_LOG}" | head -n 1 | cut -d: -f1)"
  for analyzer in coinjoin-analysis blocksci; do
    done_line="$(grep -n "\[pipeline\] stage ${analyzer}: done" "${RUN_LOG}" | head -n 1 | cut -d: -f1)"
    if [[ -z "${report_line}" || -z "${done_line}" || "${done_line}" -gt "${report_line}" ]]; then
      echo "FAIL: expected the unified report to start after ${analyzer} finished" >&2
      exit 1
    fi
  done
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

python3 - "${RUN_DIR}" "${E2E_SCENARIO_NAME}" "${E2E_PARALLEL}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
expected = sys.argv[2]
parallel = sys.argv[3] == "1"
scenario = json.loads((run_dir / "coinjoin_emulator_data" / "scenario.json").read_text(encoding="utf-8"))
report = json.loads((run_dir / "coinjoinPipeline_data" / "unified_report.json").read_text(encoding="utf-8"))

if scenario.get("name") != expected:
    raise SystemExit(f"FAIL: expected scenario name {expected}, got {scenario.get('name')!r}")

run = report.get("run") or {}
if run.get("scenario_name") != expected:
    raise SystemExit(f"FAIL: expected report scenario_name {expected}, got {run.get('scenario_name')!r}")

if parallel:
    if run.get("coinjoin_type") != "wasabi2":
        raise SystemExit(f"FAIL: expected report coinjoin_type wasabi2, got {run.get('coinjoin_type')!r}")
    summary = report.get("summary") or {}
    baseline = json.loads((run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json").read_text(encoding="utf-8"))
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
        raise SystemExit("FAIL: unified report has no analyzer agreement metrics")
PY

# Local images are built from the checked-out analyzers, which classify every
# liquidity event as the ground truth does; published images may lag behind.
LIQUIDITY_CHECKS=("${E2E_LIQUIDITY_CHECKS[@]}")
if [[ "${IMAGE_MODE}" == "local" ]]; then
  LIQUIDITY_CHECKS+=(--require-blocksci --exact-baseline)
fi
python3 "${SCRIPT_DIR}/support/check_liquidity.py" "${RUN_DIR}/coinjoinPipeline_data" "${LIQUIDITY_CHECKS[@]}"

echo "PASS: real ${WORKFLOW} runIt workflow completed and produced artifacts in ${RUN_DIR}."
