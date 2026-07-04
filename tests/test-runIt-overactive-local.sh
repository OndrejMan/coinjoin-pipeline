#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RESOURCE_DIR="${PROJECT_DIR}/src/coinjoin_pipeline/resources"
LAUNCHER="${PROJECT_DIR}/runIt.sh"
TMP_DIR="$(mktemp -d)"
EXPECTED_COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

FAKE_BIN="${TMP_DIR}/bin"
FAKE_LOGS="${TMP_DIR}/logs"
DOCKER_LOG="${TMP_DIR}/docker.args"
mkdir -p "${FAKE_BIN}" "${FAKE_LOGS}"

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${DOCKER_LOG:?}"
printf '\n' >>"${DOCKER_LOG:?}"
exit 0
EOF
chmod +x "${FAKE_BIN}/docker"

export DOCKER_LOG
export WRAPPER_IMAGE="${WRAPPER_IMAGE:-ghcr.io/ondrejman/blocksciemulatoranalysis:latest}"
export BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-ghcr.io/ondrejman/blocksci-complete:latest}"
export COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}"
export COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"

(
  cd "${PROJECT_DIR}"
  EMULATION_LOGS_DIR="${FAKE_LOGS}" \
  WRAPPER_IMAGE="ghcr.io/ondrejman/blocksciemulatoranalysis:latest" \
  WRAPPER_PULL_POLICY="" \
  COINJOIN_EMULATOR_IMAGE_PREFIX=registry.example/ \
  COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD=1 \
  PATH="${FAKE_BIN}:${PATH}" \
  bash "${LAUNCHER}" --engine wasabi --scenario scenarios/overactive-local.json
)

if [[ ! -s "${DOCKER_LOG}" ]]; then
  echo "FAIL: docker was not called" >&2
  exit 1
fi

if ! grep -q -- '^run ' "${DOCKER_LOG}"; then
  echo "FAIL: expected runIt.sh to call 'docker run'" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "^run .*--pull always " "${DOCKER_LOG}"; then
  echo "FAIL: expected docker wrapper run to pull the latest wrapper image" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

# Local-only wrapper tags (no registry prefix) must not force a registry pull,
# otherwise `docker run --pull always` fails for freshly built local images.
LOCAL_DOCKER_LOG="${TMP_DIR}/docker-local.args"
: >"${LOCAL_DOCKER_LOG}"
(
  cd "${PROJECT_DIR}"
  EMULATION_LOGS_DIR="${FAKE_LOGS}" \
  WRAPPER_IMAGE="blocksciemulatoranalysis:bitcoinanalysis-local" \
  WRAPPER_PULL_POLICY="" \
  PATH="${FAKE_BIN}:${PATH}" \
  DOCKER_LOG="${LOCAL_DOCKER_LOG}" \
  bash "${LAUNCHER}" --engine wasabi --scenario scenarios/overactive-local.json
)
if ! grep -q -- "^run .*--pull missing " "${LOCAL_DOCKER_LOG}"; then
  echo "FAIL: expected local-only wrapper image to use --pull missing" >&2
  echo "Observed: $(cat "${LOCAL_DOCKER_LOG}")" >&2
  exit 1
fi
if grep -q -- "^run .*--pull always " "${LOCAL_DOCKER_LOG}"; then
  echo "FAIL: local-only wrapper image must not force --pull always" >&2
  echo "Observed: $(cat "${LOCAL_DOCKER_LOG}")" >&2
  exit 1
fi

INVALID_POLICY_OUTPUT="${TMP_DIR}/invalid-policy.output"
if (
  cd "${PROJECT_DIR}"
  EMULATION_LOGS_DIR="${FAKE_LOGS}" \
  WRAPPER_PULL_POLICY="sometimes" \
  PATH="${FAKE_BIN}:${PATH}" \
  bash "${LAUNCHER}" --engine wasabi --scenario scenarios/overactive-local.json
) >"${INVALID_POLICY_OUTPUT}" 2>&1; then
  echo "FAIL: invalid WRAPPER_PULL_POLICY should be rejected" >&2
  exit 1
fi
if ! grep -q -- "Invalid WRAPPER_PULL_POLICY='sometimes'" "${INVALID_POLICY_OUTPUT}"; then
  echo "FAIL: expected a clear invalid pull-policy error" >&2
  echo "Observed: $(cat "${INVALID_POLICY_OUTPUT}")" >&2
  exit 1
fi

if ! grep -q -- "-e HOST_CLIENT_DIR=${RESOURCE_DIR}" "${DOCKER_LOG}"; then
  echo "FAIL: expected HOST_CLIENT_DIR to point at packaged resources" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e SCENARIOS_DIR=${RESOURCE_DIR}/scenarios" "${DOCKER_LOG}"; then
  echo "FAIL: expected SCENARIOS_DIR to point at packaged scenarios" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-v ${RESOURCE_DIR}/scenarios:/app/scenarios:ro" "${DOCKER_LOG}"; then
  echo "FAIL: expected scenarios directory to be mounted in the wrapper container" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if grep -q -- ":/app:rw" "${DOCKER_LOG}"; then
  echo "FAIL: expected wrapper image /app contents to be used without a local checkout mount" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e WRAPPER_SCRIPT=wrapper.py" "${DOCKER_LOG}"; then
  echo "FAIL: expected wrapper.py to come from the wrapper image" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e POST_WRAPPER_SHELL=0" "${DOCKER_LOG}"; then
  echo "FAIL: expected noninteractive runs not to keep the wrapper container shell open" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e BLOCKSCI_LAUNCH_JUPYTER=0" "${DOCKER_LOG}"; then
  echo "FAIL: expected noninteractive runs not to launch the Jupyter environment" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if grep -q -- "-e COMPOSE_FILE=/app/compose.yaml" "${DOCKER_LOG}"; then
  echo "FAIL: expected compose.yaml to come from the wrapper image" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e EXPORTERS_FROM_IMAGE=1" "${DOCKER_LOG}"; then
  echo "FAIL: expected exporters to be seeded from the wrapper image" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e EXPORTERS_DIR=${FAKE_LOGS}/.wrapper-exporters" "${DOCKER_LOG}"; then
  echo "FAIL: expected image exporters to be copied under the logs directory" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-v ${FAKE_LOGS}/.wrapper-exporters:${FAKE_LOGS}/.wrapper-exporters:rw" "${DOCKER_LOG}"; then
  echo "FAIL: expected image exporter destination to be mounted into the wrapper" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e PYTHONDONTWRITEBYTECODE=1" "${DOCKER_LOG}"; then
  echo "FAIL: expected wrapper container not to write Python bytecode into mounted checkout" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e COINJOIN_EMULATOR_IMAGE_PREFIX=registry.example/" "${DOCKER_LOG}"; then
  echo "FAIL: expected image prefix to be forwarded to the wrapper container" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -Fq -- "-e COINJOIN_EMULATOR_IMAGE=${EXPECTED_COINJOIN_EMULATOR_IMAGE}" "${DOCKER_LOG}"; then
  echo "FAIL: expected configured emulator image to be forwarded to the wrapper container" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD=1" "${DOCKER_LOG}"; then
  echo "FAIL: expected infrastructure local build flag to be forwarded to the wrapper container" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if grep -q -- "-e COINJOIN_EMULATOR_BTC_NODE_IMAGE" "${DOCKER_LOG}"; then
  echo "FAIL: per-image exact refs should not be forwarded at wrapper level" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if grep -q -- "-e COINJOIN_EMULATOR_JOINMARKET_CLIENT_SERVER_IMAGE" "${DOCKER_LOG}"; then
  echo "FAIL: per-image exact refs should not be forwarded at wrapper level" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if grep -q -- "-e COINJOIN_EMULATOR_IRC_SERVER_IMAGE" "${DOCKER_LOG}"; then
  echo "FAIL: per-image exact refs should not be forwarded at wrapper level" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "_ --engine wasabi --scenario scenarios/overactive-local.json --test-values" "${DOCKER_LOG}"; then
  echo "FAIL: expected exact overactive-local scenario path and default test values to be forwarded" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if grep -q -- "_ full-run " "${DOCKER_LOG}"; then
  echo "FAIL: default action should be left to wrapper.py, not injected by runIt.sh" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

PBS_BITCOIN_DATADIR="${TMP_DIR}/pbs-bitcoin"
mkdir -p "${PBS_BITCOIN_DATADIR}/regtest/blocks"
mkdir -p "${FAKE_LOGS}/run-a"
: >"${DOCKER_LOG}"

(
  cd "${PROJECT_DIR}"
  EMULATION_LOGS_DIR="${FAKE_LOGS}" \
  PATH="${FAKE_BIN}:${PATH}" \
  bash "${LAUNCHER}" analyze --engine joinmarket --run-dir run-a --blocksciPbs \
    --pbs-bitcoin-datadir "${PBS_BITCOIN_DATADIR}" --dry-run
)

if ! grep -q -- '^run ' "${DOCKER_LOG}"; then
  echo "FAIL: expected PBS stage dry-run to launch wrapper so it can render the PBS script" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-v ${PBS_BITCOIN_DATADIR}:${PBS_BITCOIN_DATADIR}:ro" "${DOCKER_LOG}"; then
  echo "FAIL: expected PBS Bitcoin datadir to be mounted into the wrapper" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "--pbs-bitcoin-datadir ${PBS_BITCOIN_DATADIR}" "${DOCKER_LOG}"; then
  echo "FAIL: expected canonical PBS Bitcoin datadir to be forwarded to wrapper.py" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

cat >"${FAKE_BIN}/kubectl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${FAKE_BIN}/kubectl"
KUBE_CONFIG="${TMP_DIR}/kubeconfig"
DIRECT_PBS_BITCOIN_DATADIR="${TMP_DIR}/direct-pbs-bitcoin"
touch "${KUBE_CONFIG}"
: >"${DOCKER_LOG}"

(
  cd "${PROJECT_DIR}"
  EMULATION_LOGS_DIR="${FAKE_LOGS}" \
  PATH="${FAKE_BIN}:${PATH}" \
  bash "${LAUNCHER}" full-run --engine joinmarket --driver kubernetes \
    --kubeconfig "${KUBE_CONFIG}" --analysisPbs --blocksciPbs \
    --pbs-bitcoin-datadir "${DIRECT_PBS_BITCOIN_DATADIR}"
)

if [[ ! -d "${DIRECT_PBS_BITCOIN_DATADIR}" ]]; then
  echo "FAIL: direct Kubernetes mode should create a fresh shared Bitcoin datadir" >&2
  exit 1
fi
if ! grep -q -- "-v ${DIRECT_PBS_BITCOIN_DATADIR}:${DIRECT_PBS_BITCOIN_DATADIR}:rw" "${DOCKER_LOG}"; then
  echo "FAIL: direct Kubernetes Bitcoin datadir must be writable in the wrapper" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi
if ! grep -q -- "-e KUBERNETES_STORAGE_UID=$(id -u) -e KUBERNETES_STORAGE_GID=$(id -g)" "${DOCKER_LOG}"; then
  echo "FAIL: direct Kubernetes mode must preserve frontend storage ownership" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

ISOLATED_PROJECT="${TMP_DIR}/isolated/bitcoinAnalysis"
ISOLATED_LOGS="${TMP_DIR}/isolated/logs"
mkdir -p "${ISOLATED_PROJECT}/container" "${ISOLATED_PROJECT}/scenarios" "${ISOLATED_PROJECT}/notebooks" "${ISOLATED_LOGS}"
cp "${PROJECT_DIR}/runIt.sh" "${ISOLATED_PROJECT}/runIt.sh"
cp "${PROJECT_DIR}/container/launcher.sh" "${ISOLATED_PROJECT}/container/launcher.sh"
cp "${PROJECT_DIR}/scenarios/overactive-local.json" "${ISOLATED_PROJECT}/scenarios/overactive-local.json"
cp -a "${PROJECT_DIR}/src" "${ISOLATED_PROJECT}/src"
: >"${DOCKER_LOG}"

(
  cd "${ISOLATED_PROJECT}"
  env -u EXPORTERS_DIR \
    EMULATION_LOGS_DIR="${ISOLATED_LOGS}" \
    PATH="${FAKE_BIN}:${PATH}" \
    bash runIt.sh --engine wasabi --scenario scenarios/overactive-local.json
)

if ! grep -q -- "-e EXPORTERS_FROM_IMAGE=1" "${DOCKER_LOG}"; then
  echo "FAIL: expected isolated run to use exporters bundled in the wrapper image" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e EXPORTERS_DIR=${ISOLATED_LOGS}/.wrapper-exporters" "${DOCKER_LOG}"; then
  echo "FAIL: expected isolated run to export image-bundled exporters under the logs directory" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-v ${ISOLATED_LOGS}:${ISOLATED_LOGS}:rw" "${DOCKER_LOG}"; then
  echo "FAIL: expected isolated logs directory to be mounted so wrapper image can seed exporters" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

SIGNAL_BIN="${TMP_DIR}/signal-bin"
SIGNAL_LOGS="${TMP_DIR}/signal-logs"
SIGNAL_DOCKER_LOG="${TMP_DIR}/signal-docker.args"
RUN_STARTED="${TMP_DIR}/signal-run.started"
SIGNAL_RUN_PID_FILE="${TMP_DIR}/signal-run.pid"
mkdir -p "${SIGNAL_BIN}" "${SIGNAL_LOGS}"

cat >"${SIGNAL_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${SIGNAL_DOCKER_LOG:?}"
printf '\n' >>"${SIGNAL_DOCKER_LOG:?}"

if [[ "$1" == "run" ]]; then
  echo "$$" >"${SIGNAL_RUN_PID_FILE:?}"
  touch "${RUN_STARTED:?}"
  while true; do sleep 1; done
fi

if [[ "$1" == "stop" && -s "${SIGNAL_RUN_PID_FILE:?}" ]]; then
  kill "$(cat "${SIGNAL_RUN_PID_FILE}")" >/dev/null 2>&1 || true
fi

exit 0
EOF
chmod +x "${SIGNAL_BIN}/docker"

export SIGNAL_DOCKER_LOG RUN_STARTED SIGNAL_RUN_PID_FILE

(
  cd "${PROJECT_DIR}"
  WRAPPER_CONTAINER_NAME="test-wrapper-interrupt" \
  EMULATION_LOGS_DIR="${SIGNAL_LOGS}" \
  PATH="${SIGNAL_BIN}:${PATH}" \
  bash "${LAUNCHER}" --engine wasabi --scenario scenarios/overactive-local.json
) &
RUN_PID=$!

for _ in {1..50}; do
  [[ -e "${RUN_STARTED}" ]] && break
  sleep 0.1
done

if [[ ! -e "${RUN_STARTED}" ]]; then
  echo "FAIL: signal test did not reach docker run" >&2
  kill "${RUN_PID}" >/dev/null 2>&1 || true
  wait "${RUN_PID}" >/dev/null 2>&1 || true
  exit 1
fi

kill -TERM "${RUN_PID}"
set +e
wait "${RUN_PID}"
RUN_EXIT_CODE=$?
set -e

if [[ "${RUN_EXIT_CODE}" -ne 130 ]]; then
  echo "FAIL: expected runIt.sh to exit 130 after TERM, got ${RUN_EXIT_CODE}" >&2
  echo "Observed: $(cat "${SIGNAL_DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "^run .*--name test-wrapper-interrupt " "${SIGNAL_DOCKER_LOG}"; then
  echo "FAIL: expected runIt.sh to name the wrapper container for cleanup" >&2
  echo "Observed: $(cat "${SIGNAL_DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "^stop test-wrapper-interrupt blocksci_analyzer coinjoin_analysis emulator_manager btc_data_wiper dind_image_prefetch isolated_docker_daemon " "${SIGNAL_DOCKER_LOG}"; then
  echo "FAIL: expected runIt.sh interrupt cleanup to stop the wrapper and emulator containers" >&2
  echo "Observed: $(cat "${SIGNAL_DOCKER_LOG}")" >&2
  exit 1
fi

echo "PASS: the in-image launcher is CI-testable, standalone, and forwards the scenario."
