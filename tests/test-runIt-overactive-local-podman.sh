#!/usr/bin/env bash
# Happy-path E2E: run the complete Wasabi pipeline through host Podman.
#
# Unlike test-podman-no-host-docker.sh, this starts real containers.  The
# wrapper uses Podman Compose; the emulator's Docker-in-Docker daemon remains
# inside that stack. A fake docker binary rejects host Docker fallback.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROJECT_DIR}/.." && pwd)"
LOGS_DIR="${EMULATION_LOGS_DIR:-${PROJECT_DIR}/emulation_logs}"
TMP_DIR="$(mktemp -d)"
BEFORE_FILE="${TMP_DIR}/runs-before"
AFTER_FILE="${TMP_DIR}/runs-after"
RUN_LOG="${TMP_DIR}/pipeline.log"
FAKE_BIN="${TMP_DIR}/bin"
DOCKER_LOG="${TMP_DIR}/docker.called"
RUN_PID=""
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-3600}"
IMAGE_MODE="${1:-upstream}"
COMPOSE_PROJECT="cjp-overactive-local-podman-${RANDOM}"

if [[ $# -gt 1 || ( "${IMAGE_MODE}" != "upstream" && "${IMAGE_MODE}" != "local" ) ]]; then
  echo "Usage: $0 [local]" >&2
  echo "Pass no argument for published images, or 'local' for Podman-built local images." >&2
  exit 2
fi

LOCAL_TAG="${LOCAL_TAG:-coinjoin-pipeline-podman-local}"
LOCAL_BLOCKSCI_BASE_IMAGE="${LOCAL_BLOCKSCI_BASE_IMAGE:-blocksci-cj:${LOCAL_TAG}}"
LOCAL_BLOCKSCI_IMAGE="${LOCAL_BLOCKSCI_IMAGE:-blocksci-complete:${LOCAL_TAG}}"
LOCAL_EMULATOR_IMAGE="${LOCAL_EMULATOR_IMAGE:-coinjoin-emulator:${LOCAL_TAG}}"
LOCAL_COINJOIN_ANALYSIS_IMAGE="${LOCAL_COINJOIN_ANALYSIS_IMAGE:-coinjoin-analysis:${LOCAL_TAG}}"
BLOCKSCI_SOURCE_DIR="${BLOCKSCI_SOURCE_DIR:-${REPO_ROOT}/blocksci}"
EMULATOR_SOURCE_DIR="${EMULATOR_SOURCE_DIR:-${REPO_ROOT}/coinjoin-emulator}"
COINJOIN_ANALYSIS_SOURCE_DIR="${COINJOIN_ANALYSIS_SOURCE_DIR:-${REPO_ROOT}/coinjoin-analysis}"
LOCAL_IMAGES_PREBUILT="${LOCAL_IMAGES_PREBUILT:-0}"

stop_pipeline() {
  podman compose -f "${PROJECT_DIR}/pipeline/compose.yaml" -p "${COMPOSE_PROJECT}" \
    --profile emulate --profile analysis down --remove-orphans >/dev/null 2>&1 || true
}

cleanup() {
  if [[ -n "${RUN_PID}" ]] && kill -0 "${RUN_PID}" >/dev/null 2>&1; then
    stop_pipeline
    wait "${RUN_PID}" >/dev/null 2>&1 || true
  fi
  stop_pipeline
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

list_run_dirs() {
  find "${LOGS_DIR}" -mindepth 1 -maxdepth 1 -type d \
    -exec test -s "{}/coinjoin_emulator_data/scenario.json" \; -print |
    LC_ALL=C sort
}

if ! command -v podman >/dev/null 2>&1; then
  echo "FAIL: podman command not found" >&2
  exit 2
fi
if ! podman info >/dev/null 2>&1; then
  echo "FAIL: Podman service is not reachable" >&2
  exit 2
fi
if ! podman compose version >/dev/null 2>&1; then
  echo "FAIL: podman compose is not available" >&2
  exit 2
fi

mkdir -p "${FAKE_BIN}" "${LOGS_DIR}"
cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
echo "FAIL: pipeline invoked host Docker: docker $*" >&2
touch "${DOCKER_LOG:?}"
exit 99
EOF
chmod +x "${FAKE_BIN}/docker"
export DOCKER_LOG

list_run_dirs >"${BEFORE_FILE}"

if [[ "${IMAGE_MODE}" == "local" ]]; then
  if [[ "${LOCAL_IMAGES_PREBUILT}" != "1" ]]; then
    echo "Building local BlockSci images with Podman..."
    podman build -t "${LOCAL_BLOCKSCI_BASE_IMAGE}" -f "${BLOCKSCI_SOURCE_DIR}/Dockerfile" "${BLOCKSCI_SOURCE_DIR}"
    podman build --build-arg "BLOCKSCI_BASE_IMAGE=${LOCAL_BLOCKSCI_BASE_IMAGE}" \
      -t "${LOCAL_BLOCKSCI_IMAGE}" -f "${BLOCKSCI_SOURCE_DIR}/Dockerfile_complete" "${BLOCKSCI_SOURCE_DIR}"
    echo "Building local emulator and analysis images with Podman..."
    podman build -t "${LOCAL_EMULATOR_IMAGE}" "${EMULATOR_SOURCE_DIR}"
    podman build -t "${LOCAL_COINJOIN_ANALYSIS_IMAGE}" \
      -f "${COINJOIN_ANALYSIS_SOURCE_DIR}/docker/analysis.Dockerfile" "${COINJOIN_ANALYSIS_SOURCE_DIR}"
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
  COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"
  COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}"
  BLOCKSCI_PULL_POLICY="${BLOCKSCI_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY:-always}"
  COINJOIN_ANALYSIS_PULL_POLICY="${COINJOIN_ANALYSIS_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_IMAGE_PREFIX="${COINJOIN_EMULATOR_IMAGE_PREFIX:-ghcr.io/ondrejman/}"
  COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:-}"
fi

echo "Running complete Podman workflow with logs in ${LOGS_DIR}..."
(
  (
    cd "${PROJECT_DIR}"
    PATH="${FAKE_BIN}:${PATH}" \
    EMULATION_LOGS_DIR="${LOGS_DIR}" \
    CONTAINER_RUNTIME=podman \
    COINJOIN_COMPOSE_PROJECT="${COMPOSE_PROJECT}" \
    BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
    BLOCKSCI_PULL_POLICY="${BLOCKSCI_PULL_POLICY}" \
    COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
    COINJOIN_ANALYSIS_PULL_POLICY="${COINJOIN_ANALYSIS_PULL_POLICY}" \
    COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
    COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY}" \
    COINJOIN_EMULATOR_IMAGE_PREFIX="${COINJOIN_EMULATOR_IMAGE_PREFIX}" \
    COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD}" \
    bash runIt.sh container podman full-run \
      --engine wasabi \
      --scenario scenarios/overactive-local.json \
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
    echo "FAIL: noninteractive pipeline launched the BlockSci Jupyter server" >&2
    stop_pipeline
    wait "${RUN_PID}" >/dev/null 2>&1 || true
    RUN_PID=""
    exit 1
  fi
  sleep 1
done

if [[ "${run_finished}" == "false" ]] && kill -0 "${RUN_PID}" >/dev/null 2>&1; then
  echo "FAIL: Podman pipeline did not exit within ${RUN_TIMEOUT_SECONDS}s" >&2
  stop_pipeline
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
  echo "FAIL: Podman pipeline exited with code ${RUN_EXIT_CODE}" >&2
  exit "${RUN_EXIT_CODE}"
fi
if [[ -e "${DOCKER_LOG}" ]]; then
  echo "FAIL: the Podman E2E path invoked host Docker" >&2
  exit 1
fi
if ! grep -q "Parsing complete. Skipping interactive BlockSci environment." "${RUN_LOG}"; then
  echo "FAIL: expected BlockSci analysis to finish noninteractively" >&2
  exit 1
fi

list_run_dirs >"${AFTER_FILE}"
RUN_DIR="$(LC_ALL=C comm -13 "${BEFORE_FILE}" "${AFTER_FILE}" | tail -n 1)"
if [[ -z "${RUN_DIR}" || ! -d "${RUN_DIR}" ]]; then
  echo "FAIL: no overactive-local run directory was created under ${LOGS_DIR}" >&2
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

python3 - "${RUN_DIR}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
scenario = json.loads((run_dir / "coinjoin_emulator_data" / "scenario.json").read_text(encoding="utf-8"))
report = json.loads((run_dir / "coinjoinPipeline_data" / "unified_report.json").read_text(encoding="utf-8"))

if scenario.get("name") != "overactive-local":
    raise SystemExit(f"FAIL: expected scenario name overactive-local, got {scenario.get('name')!r}")
if (report.get("run") or {}).get("scenario_name") != "overactive-local":
    raise SystemExit("FAIL: report does not belong to overactive-local")
PY

echo "PASS: complete Podman pipeline produced the expected emulator, analysis, BlockSci, and report artifacts in ${RUN_DIR}."
