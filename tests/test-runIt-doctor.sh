#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCHER="${PROJECT_DIR}/runIt.sh"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

FAKE_BIN="${TMP_DIR}/bin"
FAKE_LOG="${TMP_DIR}/docker.args"
FAKE_LOGS="${TMP_DIR}/logs"
mkdir -p "${FAKE_BIN}" "${FAKE_LOGS}"

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${DOCKER_LOG:?}"
printf '\n' >>"${DOCKER_LOG:?}"

if [[ "$1" == "info" && "${FAIL_INFO:-0}" == "1" ]]; then
  exit 1
fi
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  [[ "${LOCAL_IMAGES:-0}" == "1" ]] && exit 0
  exit 1
fi
if [[ "$1" == "manifest" && "$2" == "inspect" && "${FAIL_MANIFEST:-0}" == "1" ]]; then
  exit 1
fi
exit 0
EOF
chmod +x "${FAKE_BIN}/docker"

export DOCKER_LOG="${FAKE_LOG}"

run_it() {
  : >"${FAKE_LOG}"
  (
  cd "${PROJECT_DIR}"
    EMULATION_LOGS_DIR="${FAKE_LOGS}" \
    WRAPPER_IMAGE="ghcr.io/ondrejman/blocksciemulatoranalysis:latest" \
    BLOCKSCI_IMAGE="ghcr.io/ondrejman/blocksci-complete:latest" \
    COINJOIN_EMULATOR_IMAGE="ghcr.io/ondrejman/coinjoin-emulator:latest" \
    COINJOIN_ANALYSIS_IMAGE="ghcr.io/ondrejman/coinjoin-analysis:latest" \
    PATH="${FAKE_BIN}:${PATH}" \
    "$@"
  )
}

# A successful normal run executes doctor first and then launches the wrapper.
run_it bash "${LAUNCHER}" --engine wasabi --scenario scenarios/overactive-local.json
grep -q '^info ' "${FAKE_LOG}"
grep -q '^manifest inspect ghcr.io/ondrejman/blocksciemulatoranalysis:latest ' "${FAKE_LOG}"
grep -q '^run ' "${FAKE_LOG}"

# Dry runs perform the same checks but never launch the wrapper container.
run_it bash "${LAUNCHER}" --engine wasabi --scenario scenarios/overactive-local.json --dry-run
grep -q '^manifest inspect ghcr.io/ondrejman/blocksciemulatoranalysis:latest ' "${FAKE_LOG}"
if grep -q '^run ' "${FAKE_LOG}"; then
  echo "FAIL: doctor-only dry run launched the wrapper" >&2
  exit 1
fi

# A globally exported PBS datadir is ignored unless the BlockSci PBS stage is requested.
run_it env PBS_BITCOIN_DATADIR="${TMP_DIR}/missing-pbs-datadir" bash "${LAUNCHER}" \
  --engine wasabi --scenario scenarios/overactive-local.json --dry-run >"${TMP_DIR}/pbs-env.out" 2>&1
grep -q '\[dry-run\]' "${TMP_DIR}/pbs-env.out"
if grep -q 'PBS Bitcoin datadir' "${TMP_DIR}/pbs-env.out"; then
  echo "FAIL: non-PBS dry-run should not validate PBS_BITCOIN_DATADIR" >&2
  exit 1
fi

# A runtime failure and a strict scenario failure both prevent launch.
set +e
FAIL_INFO=1 run_it bash "${LAUNCHER}" --engine wasabi --scenario scenarios/overactive-local.json >"${TMP_DIR}/daemon.out" 2>&1
daemon_exit=$?
set -e
[[ "${daemon_exit}" -ne 0 ]]
grep -q 'daemon/API is not reachable' "${TMP_DIR}/daemon.out"
if grep -q '^run ' "${FAKE_LOG}"; then
  echo "FAIL: daemon failure launched the wrapper" >&2
  exit 1
fi

set +e
run_it bash "${LAUNCHER}" --engine wasabi --scenario scenarios/missing.json >"${TMP_DIR}/scenario.out" 2>&1
scenario_exit=$?
set -e
[[ "${scenario_exit}" -ne 0 ]]
if grep -q '^run ' "${FAKE_LOG}"; then
  echo "FAIL: invalid scenario launched the wrapper" >&2
  exit 1
fi

# Images may be supplied locally; otherwise an inaccessible registry blocks execution.
mkdir -p "${FAKE_LOGS}/existing-run"
run_it env LOCAL_IMAGES=1 bash "${LAUNCHER}" export --engine wasabi --run-dir existing-run
grep -q '^image inspect ghcr.io/ondrejman/blocksciemulatoranalysis:latest ' "${FAKE_LOG}"
if grep -q '^manifest inspect ' "${FAKE_LOG}"; then
  echo "FAIL: locally available image was unnecessarily resolved from registry" >&2
  exit 1
fi

set +e
FAIL_MANIFEST=1 run_it bash "${LAUNCHER}" export --engine wasabi --run-dir existing-run >"${TMP_DIR}/image.out" 2>&1
image_exit=$?
set -e
[[ "${image_exit}" -ne 0 ]]
grep -q 'image is unavailable locally and from its registry' "${TMP_DIR}/image.out"
if grep -q '^run ' "${FAKE_LOG}"; then
  echo "FAIL: inaccessible image launched the wrapper" >&2
  exit 1
fi

# Kubernetes preflight fails before launch when the kubeconfig is missing.
set +e
run_it bash "${LAUNCHER}" recreate --engine wasabi --scenario scenarios/overactive-local.json --driver kubernetes --kubeconfig "${TMP_DIR}/missing-kubeconfig" >"${TMP_DIR}/kube.out" 2>&1
kube_exit=$?
set -e
[[ "${kube_exit}" -ne 0 ]]
grep -q 'kubeconfig not found' "${TMP_DIR}/kube.out"
if grep -q '^run ' "${FAKE_LOG}"; then
  echo "FAIL: Kubernetes preflight failure launched the wrapper" >&2
  exit 1
fi

echo "PASS: runIt.sh doctor gates pipeline actions without creating runtime resources."
