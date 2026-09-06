#!/usr/bin/env bash
# Dry, container-free checks of the bare runtime command that replaced the
# in-image launcher. Everything here asserts on the host CLI's rendered
# invocation, so it stays fast and needs no images.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHER="${PROJECT_DIR}/runIt.sh"
RUNTIME_DIR="${PROJECT_DIR}/pipeline"
TMP_DIR="$(mktemp -d)"
EXPECTED_COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

FAKE_BIN="${TMP_DIR}/bin"
FAKE_LOGS="${TMP_DIR}/logs"
RENDERED="${TMP_DIR}/rendered.txt"
mkdir -p "${FAKE_BIN}" "${FAKE_LOGS}"

# The host preflight probes the container runtime; a stub keeps this test
# independent of a real daemon.
cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${FAKE_BIN}/docker"
cp "${FAKE_BIN}/docker" "${FAKE_BIN}/kubectl"

export BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-ghcr.io/ondrejman/blocksci-complete:latest}"
export COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}"
export COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"

fail_with() {
  echo "FAIL: $1" >&2
  echo "Observed: $(cat "${RENDERED}")" >&2
  exit 1
}

expect() {
  grep -Fq -- "$1" "${RENDERED}" || fail_with "$2"
}

refute() {
  grep -Fq -- "$1" "${RENDERED}" && fail_with "$2"
  return 0
}

# Unlike the old launcher test, the fake docker no longer swallows the wrapper:
# it now really runs, so stage dry-runs may exit non-zero for reasons unrelated
# to the rendered command. Capture the status instead of tripping `set -e`.
RENDER_STATUS=0
render() {
  RENDER_STATUS=0
  (
    cd "${PROJECT_DIR}"
    EMULATION_LOGS_DIR="${FAKE_LOGS}" \
    PATH="${FAKE_BIN}:${PATH}" \
    bash "${LAUNCHER}" "$@"
  ) >"${RENDERED}" 2>&1 || RENDER_STATUS=$?
}

# --- default local run -------------------------------------------------------
render --engine wasabi --scenario scenarios/overactive-local.json \
  --dry-run

expect "pipeline/client/wrapper.py" \
  "expected the CLI to run the wrapper directly from the checkout"
refute "docker run" \
  "the wrapper must no longer be started through a container"

expect "HOST_CLIENT_DIR=${RUNTIME_DIR}/client" \
  "expected HOST_CLIENT_DIR to point at the checkout runtime"
expect "EXPORTERS_DIR=${RUNTIME_DIR}/exporters" \
  "expected exporters to be used straight from the checkout"
expect "SCENARIOS_DIR=${PROJECT_DIR}/scenarios" \
  "expected the root scenarios tree, not pipeline/client/scenarios"
expect "NOTEBOOKS_DIR=${FAKE_LOGS}/.notebooks" \
  "expected notebooks to live beside the run evidence"
expect "EMULATION_LOGS_DIR=${FAKE_LOGS}" \
  "expected the runs root to be forwarded"
expect "PYTHONPATH=${RUNTIME_DIR}" \
  "expected an explicit PYTHONPATH instead of relying on wrapper.py's sys.path hack"

# Defaults the deleted launcher used to compute.
expect "BLOCKSCI_LAUNCH_JUPYTER=0" \
  "noninteractive runs must not launch the interactive BlockSci environment"
expect "PYTHONDONTWRITEBYTECODE=1" \
  "the bare wrapper must not write bytecode into the checkout"

expect "COINJOIN_EMULATOR_IMAGE=${EXPECTED_COINJOIN_EMULATOR_IMAGE}" \
  "expected the pinned emulator image to be forwarded"
refute "WRAPPER_IMAGE" \
  "the wrapper image must be gone from the environment contract"
refute "POST_WRAPPER_SHELL" \
  "POST_WRAPPER_SHELL disappeared with the wrapper container"
refute "EXPORTERS_FROM_IMAGE" \
  "exporters are no longer seeded from an image"

expect "--engine wasabi --scenario scenarios/overactive-local.json" \
  "expected the scenario path to be forwarded verbatim"
refute "--test-values" \
  "removed BlockSci test-values option must not be rendered"
refute "wrapper.py full-run" \
  "the default action is wrapper.py's job, not the host CLI's"

# --- relative paths keep resolving from the user's directory -----------------
OUTSIDE_DIR="${TMP_DIR}/outside"
mkdir -p "${OUTSIDE_DIR}"
(
  cd "${OUTSIDE_DIR}"
  EMULATION_LOGS_DIR="${FAKE_LOGS}" \
  PATH="${FAKE_BIN}:${PATH}" \
  bash "${LAUNCHER}" --engine wasabi \
    --scenario "${PROJECT_DIR}/scenarios/overactive-local.json" --dry-run
) >"${RENDERED}" 2>&1 || true
expect "SCENARIOS_DIR=${PROJECT_DIR}/scenarios" \
  "the runtime contract must not depend on the caller's working directory"

# --- PBS Bitcoin datadir validation -----------------------------------------
PBS_BITCOIN_DATADIR="${TMP_DIR}/pbs-bitcoin"
mkdir -p "${PBS_BITCOIN_DATADIR}/regtest/blocks"
mkdir -p "${FAKE_LOGS}/run-a"

render analyze --engine joinmarket --run-dir run-a --blocksciPbs \
  --pbs-bitcoin-datadir "${PBS_BITCOIN_DATADIR}" --dry-run
expect "--pbs-bitcoin-datadir ${PBS_BITCOIN_DATADIR}" \
  "expected the PBS Bitcoin datadir to be forwarded to wrapper.py"

INCOMPLETE_DATADIR="${TMP_DIR}/pbs-bitcoin-empty"
mkdir -p "${INCOMPLETE_DATADIR}"
render analyze --engine joinmarket --run-dir run-a --blocksciPbs \
  --pbs-bitcoin-datadir "${INCOMPLETE_DATADIR}" --dry-run
if [[ "${RENDER_STATUS}" -eq 0 ]]; then
  fail_with "a PBS datadir without regtest/blocks must be rejected"
fi
expect "must contain regtest/blocks" \
  "expected a clear error for an unusable PBS Bitcoin datadir"

# --- Kubernetes copy-to-host default ----------------------------------------
KUBE_CONFIG="${TMP_DIR}/kubeconfig"
touch "${KUBE_CONFIG}"
render emulate --engine wasabi --driver kubernetes \
  --kubeconfig "${KUBE_CONFIG}" --copy-to-host --dry-run
expect "KUBERNETES_COPY_TO_HOST_DIR=${FAKE_LOGS}/.kubernetes-btc-data" \
  "expected the launcher's copy-to-host default to be carried over"

# --- no PBS_FRONTEND_DIRECT switch anywhere ---------------------------------
render full-run --engine wasabi --driver kubernetes --artifact-backend s3 \
  --artifact-uri s3://bucket/runs --s3-endpoint-url https://s3.example.invalid \
  --s3-secret-name coinjoin-s3 --s3-credentials-file "${KUBE_CONFIG}" \
  --s3-profile coinjoin --run-id run-s3 --reuse-namespace \
  --kubeconfig "${KUBE_CONFIG}" --analysisPbs --blocksciPbs --dry-run
refute "PBS_FRONTEND_DIRECT" \
  "the S3 path must work without any environment switch"

echo "PASS: the host CLI renders a bare, checkout-backed wrapper invocation."
