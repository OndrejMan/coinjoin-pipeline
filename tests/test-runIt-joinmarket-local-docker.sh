#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOGS_DIR="${EMULATION_LOGS_DIR:-${PROJECT_DIR}/emulation_logs}"
BEFORE_FILE="$(mktemp)"
AFTER_FILE="$(mktemp)"
RUN_LOG="$(mktemp)"
RUN_PID=""
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-3600}"
REPO_ROOT="$(cd "${PROJECT_DIR}/.." && pwd)"
IMAGE_MODE="${1:-upstream}"
if [[ $# -gt 1 ]]; then
  echo "Usage: $0 [local]" >&2
  exit 2
fi
if [[ "${IMAGE_MODE}" != "upstream" && "${IMAGE_MODE}" != "local" ]]; then
  echo "Usage: $0 [local]" >&2
  echo "Pass no argument to use upstream images, or 'local' to build local images first." >&2
  exit 2
fi
if [[ $# -eq 0 && -n "${BUILD_LOCAL_IMAGES:-}" ]]; then
  if [[ "${BUILD_LOCAL_IMAGES}" == "0" ]]; then
    IMAGE_MODE="upstream"
  else
    IMAGE_MODE="local"
  fi
fi
BUILD_LOCAL_IMAGES=0
if [[ "${IMAGE_MODE}" == "local" ]]; then
  BUILD_LOCAL_IMAGES=1
fi
UPSTREAM_WRAPPER_IMAGE="${UPSTREAM_WRAPPER_IMAGE:-ghcr.io/ondrejman/coinjoin-pipeline:latest}"
UPSTREAM_BLOCKSCI_IMAGE="${UPSTREAM_BLOCKSCI_IMAGE:-ghcr.io/ondrejman/blocksci-complete:latest}"
UPSTREAM_EMULATOR_IMAGE="${UPSTREAM_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"
UPSTREAM_COINJOIN_ANALYSIS_IMAGE="${UPSTREAM_COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}"
LOCAL_WRAPPER_IMAGE="${LOCAL_WRAPPER_IMAGE:-coinjoin-pipeline:joinmarket-local}"
LOCAL_BLOCKSCI_IMAGE="${LOCAL_BLOCKSCI_IMAGE:-blocksci-complete:joinmarket-local}"
LOCAL_EMULATOR_IMAGE="${LOCAL_EMULATOR_IMAGE:-coinjoin-emulator:joinmarket-local}"
LOCAL_COINJOIN_ANALYSIS_IMAGE="${LOCAL_COINJOIN_ANALYSIS_IMAGE:-coinjoin-analysis:joinmarket-local}"
WRAPPER_SOURCE_DIR="${WRAPPER_SOURCE_DIR:-${PROJECT_DIR}}"
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

if [[ "${BUILD_LOCAL_IMAGES}" != "0" ]]; then
  echo "Building local BlockSci image ${LOCAL_BLOCKSCI_IMAGE} from ${BLOCKSCI_SOURCE_DIR}..."
  docker build -t "${LOCAL_BLOCKSCI_IMAGE}" "${BLOCKSCI_SOURCE_DIR}"

  echo "Building local JoinMarket emulator image ${LOCAL_EMULATOR_IMAGE} from ${EMULATOR_SOURCE_DIR}..."
  docker build -t "${LOCAL_EMULATOR_IMAGE}" "${EMULATOR_SOURCE_DIR}"

  echo "Building local wrapper image ${LOCAL_WRAPPER_IMAGE} from ${WRAPPER_SOURCE_DIR}..."
  docker build -t "${LOCAL_WRAPPER_IMAGE}" -f "${WRAPPER_SOURCE_DIR}/Dockerfile" "${WRAPPER_SOURCE_DIR}"

  echo "Building local coinjoin-analysis image ${LOCAL_COINJOIN_ANALYSIS_IMAGE} from ${COINJOIN_ANALYSIS_SOURCE_DIR}..."
  docker build -t "${LOCAL_COINJOIN_ANALYSIS_IMAGE}" -f "${COINJOIN_ANALYSIS_SOURCE_DIR}/docker/analysis.Dockerfile" "${COINJOIN_ANALYSIS_SOURCE_DIR}"
fi

if [[ "${IMAGE_MODE}" == "local" ]]; then
  WRAPPER_IMAGE="${WRAPPER_IMAGE:-${LOCAL_WRAPPER_IMAGE}}"
  BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-${LOCAL_BLOCKSCI_IMAGE}}"
  COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-${LOCAL_EMULATOR_IMAGE}}"
  COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-${LOCAL_COINJOIN_ANALYSIS_IMAGE}}"
  BLOCKSCI_PULL_POLICY="${BLOCKSCI_PULL_POLICY:-never}"
  COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY:-never}"
  COINJOIN_ANALYSIS_PULL_POLICY="${COINJOIN_ANALYSIS_PULL_POLICY:-never}"
  COINJOIN_EMULATOR_IMAGE_PREFIX=""
  COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:-1}"
else
  WRAPPER_IMAGE="${WRAPPER_IMAGE:-${UPSTREAM_WRAPPER_IMAGE}}"
  BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-${UPSTREAM_BLOCKSCI_IMAGE}}"
  COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-${UPSTREAM_EMULATOR_IMAGE}}"
  COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-${UPSTREAM_COINJOIN_ANALYSIS_IMAGE}}"
  BLOCKSCI_PULL_POLICY="${BLOCKSCI_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY:-always}"
  COINJOIN_ANALYSIS_PULL_POLICY="${COINJOIN_ANALYSIS_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_IMAGE_PREFIX="${COINJOIN_EMULATOR_IMAGE_PREFIX:-ghcr.io/ondrejman/}"
fi

echo "Running real JoinMarket Docker workflow in ${IMAGE_MODE} image mode with logs in ${LOGS_DIR}..."
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
    COINJOIN_EMULATOR_BTC_NODE_IMAGE="${COINJOIN_EMULATOR_BTC_NODE_IMAGE:-}" \
    COINJOIN_EMULATOR_JOINMARKET_CLIENT_SERVER_IMAGE="${COINJOIN_EMULATOR_JOINMARKET_CLIENT_SERVER_IMAGE:-}" \
    COINJOIN_EMULATOR_IRC_SERVER_IMAGE="${COINJOIN_EMULATOR_IRC_SERVER_IMAGE:-}" \
    COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:-}" \
    bash runIt.sh full-run --engine joinmarket
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

list_run_dirs >"${AFTER_FILE}"

RUN_DIR="$(LC_ALL=C comm -13 "${BEFORE_FILE}" "${AFTER_FILE}" | tail -n 1)"
if [[ -z "${RUN_DIR}" ]]; then
  RUN_DIR="$(find "${LOGS_DIR}" -mindepth 1 -maxdepth 1 -type d -name '*default-joinmarket*' \
    -exec test -s "{}/coinjoin_emulator_data/scenario.json" \; -print | LC_ALL=C sort | tail -n 1)"
fi

if [[ -z "${RUN_DIR}" || ! -d "${RUN_DIR}" ]]; then
  echo "FAIL: no default-joinmarket run directory was created under ${LOGS_DIR}" >&2
  exit 1
fi

for artifact in \
  coinjoin_emulator_data/scenario.json \
  coinjoin-analysis_data/coinjoin_tx_info.json \
  blocksciEmulatorAnalysis_data/unified_report.json \
  blocksciEmulatorAnalysis_data/unified_report.md \
  blocksciEmulatorAnalysis_data/emulator_data.json \
  blocksci_data/config.json; do
  if [[ ! -s "${RUN_DIR}/${artifact}" ]]; then
    echo "FAIL: expected artifact missing or empty: ${RUN_DIR}/${artifact}" >&2
    exit 1
  fi
done

if [[ ! -s "${RUN_DIR}/coinjoin_emulator_data/data/joinmarket_round_events.json" && ! -s "${RUN_DIR}/coinjoin_emulator_data/joinmarket_round_events.json" ]]; then
  echo "FAIL: expected JoinMarket round events under coinjoin_emulator_data" >&2
  exit 1
fi

python3 - "${RUN_DIR}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
scenario = json.loads((run_dir / "coinjoin_emulator_data" / "scenario.json").read_text(encoding="utf-8"))
report = json.loads((run_dir / "blocksciEmulatorAnalysis_data" / "unified_report.json").read_text(encoding="utf-8"))
coinjoin_info = json.loads((run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json").read_text(encoding="utf-8"))
emulator_data = json.loads((run_dir / "blocksciEmulatorAnalysis_data" / "emulator_data.json").read_text(encoding="utf-8"))
round_events_path = run_dir / "coinjoin_emulator_data" / "data" / "joinmarket_round_events.json"
if not round_events_path.exists():
    round_events_path = run_dir / "coinjoin_emulator_data" / "joinmarket_round_events.json"
round_events = json.loads(round_events_path.read_text(encoding="utf-8"))

if scenario.get("name") != "default-joinmarket":
    raise SystemExit(f"FAIL: expected scenario name default-joinmarket, got {scenario.get('name')!r}")
if scenario.get("default_version") != "joinmarket":
    raise SystemExit(f"FAIL: expected JoinMarket scenario default_version, got {scenario.get('default_version')!r}")

roles = {
    wallet.get("joinmarket", {}).get("role")
    for wallet in scenario.get("wallets", [])
    if isinstance(wallet.get("joinmarket"), dict)
}
if not {"maker", "taker"}.issubset(roles):
    raise SystemExit(f"FAIL: expected JoinMarket maker and taker wallets, got roles {sorted(roles)!r}")
makers = [
    wallet
    for wallet in scenario.get("wallets", [])
    if isinstance(wallet.get("joinmarket"), dict)
    and wallet.get("joinmarket", {}).get("role") == "maker"
]
takers = [
    wallet
    for wallet in scenario.get("wallets", [])
    if isinstance(wallet.get("joinmarket"), dict)
    and wallet.get("joinmarket", {}).get("role") == "taker"
]
if scenario.get("rounds", 0) < 3:
    raise SystemExit(f"FAIL: expected at least 3 JoinMarket target rounds, got {scenario.get('rounds')!r}")
if len(makers) < 8 or len(takers) < 2:
    raise SystemExit(
        "FAIL: expected surplus JoinMarket participants, "
        f"got {len(takers)} taker(s) and {len(makers)} maker(s)"
    )

run = report.get("run") or {}
if run.get("scenario_name") != "default-joinmarket":
    raise SystemExit(f"FAIL: expected report scenario_name default-joinmarket, got {run.get('scenario_name')!r}")
if run.get("coinjoin_type") != "joinmarket":
    raise SystemExit(f"FAIL: expected report coinjoin_type joinmarket, got {run.get('coinjoin_type')!r}")
if run.get("joinmarket_detector") != "definite":
    raise SystemExit(f"FAIL: expected default JoinMarket detector definite, got {run.get('joinmarket_detector')!r}")

execution = ((report.get("run_manifest") or {}).get("execution") or {})
if execution.get("engine") != "joinmarket":
    raise SystemExit(f"FAIL: expected execution engine joinmarket, got {execution.get('engine')!r}")
if execution.get("coinjoin_type") != "joinmarket":
    raise SystemExit(f"FAIL: expected execution coinjoin_type joinmarket, got {execution.get('coinjoin_type')!r}")

summary = report.get("summary") or {}
emulator_summary = emulator_data.get("summary") or {}
coinjoin_count = len(coinjoin_info.get("coinjoins") or {})
confirmed_round_events = [
    event for event in round_events
    if event.get("status") == "confirmed" and event.get("txid")
]
if not confirmed_round_events:
    raise SystemExit("FAIL: expected at least one confirmed JoinMarket round event")
if coinjoin_count < 1:
    raise SystemExit("FAIL: expected coinjoin-analysis to mark at least one JoinMarket transaction")
if summary.get("coinjoin_analysis_coinjoins", 0) < 1:
    raise SystemExit(
        "FAIL: expected unified report to include at least one coinjoin-analysis JoinMarket transaction"
    )
if emulator_summary.get("coinjoin_transactions", 0) < 1:
    raise SystemExit("FAIL: expected emulator_data to label at least one JoinMarket transaction")
PY

echo "PASS: real JoinMarket Docker runIt workflow completed and produced artifacts in ${RUN_DIR}."
