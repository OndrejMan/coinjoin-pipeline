#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  if [[ -n "${SOCKET_PID:-}" ]]; then
    kill "${SOCKET_PID}" >/dev/null 2>&1 || true
    wait "${SOCKET_PID}" >/dev/null 2>&1 || true
  fi
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

FAKE_BIN="${TMP_DIR}/bin"
FAKE_LOGS="${TMP_DIR}/logs"
SOCKET_PATH="${TMP_DIR}/podman.sock"
PODMAN_LOG="${TMP_DIR}/podman.args"
DOCKER_LOG="${TMP_DIR}/docker.called"
KUBECTL_LOG="${TMP_DIR}/kubectl.args"
mkdir -p "${FAKE_BIN}" "${FAKE_LOGS}"

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
echo "FAIL: host docker command was called: docker $*" >&2
touch "${DOCKER_LOG:?}"
exit 99
EOF
chmod +x "${FAKE_BIN}/docker"

cat >"${FAKE_BIN}/podman" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${PODMAN_LOG:?}"
printf '\n' >>"${PODMAN_LOG:?}"
exit 0
EOF
chmod +x "${FAKE_BIN}/podman"

cat >"${FAKE_BIN}/kubectl" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${KUBECTL_LOG:?}"
printf '\n' >>"${KUBECTL_LOG:?}"
exit 0
EOF
chmod +x "${FAKE_BIN}/kubectl"

export DOCKER_LOG PODMAN_LOG KUBECTL_LOG
export WRAPPER_IMAGE="${WRAPPER_IMAGE:-ghcr.io/ondrejman/coinjoin-pipeline:latest}"
export BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-ghcr.io/ondrejman/blocksci-complete:latest}"
export COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}"
export COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"

python3 - "${SOCKET_PATH}" <<'PY' &
import socket
import sys
import time

path = sys.argv[1]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
time.sleep(30)
PY
SOCKET_PID=$!

for _ in {1..50}; do
  [[ -S "${SOCKET_PATH}" ]] && break
  sleep 0.1
done

if [[ ! -S "${SOCKET_PATH}" ]]; then
  echo "FAIL: could not create temporary Podman socket at ${SOCKET_PATH}" >&2
  exit 1
fi

(
  cd "${PROJECT_DIR}"
  CONTAINER_SOCKET="${SOCKET_PATH}" \
  EMULATION_LOGS_DIR="${FAKE_LOGS}" \
  PATH="${FAKE_BIN}:${PATH}" \
  ./runIt.sh container podman --scenario overactive-local.json --engine wasabi
)

if [[ -e "${DOCKER_LOG}" ]]; then
  echo "FAIL: host docker was used" >&2
  exit 1
fi

if [[ ! -s "${PODMAN_LOG}" ]]; then
  echo "FAIL: podman was not called" >&2
  exit 1
fi

if ! grep -q -- '^run ' "${PODMAN_LOG}"; then
  echo "FAIL: expected runIt.sh to call 'podman run'" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if grep -q -- "--pull always" "${PODMAN_LOG}" || grep -q -- "--pull=always" "${PODMAN_LOG}"; then
  echo "FAIL: expected Podman wrapper run not to receive Docker-specific pull flags" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-v ${SOCKET_PATH}:/var/run/docker.sock" "${PODMAN_LOG}"; then
  echo "FAIL: expected Podman socket to be mounted as /var/run/docker.sock" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e CONTAINER_RUNTIME=docker" "${PODMAN_LOG}"; then
  echo "FAIL: expected inner wrapper runtime to be docker CLI syntax" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e DOCKER_HOST=unix:///var/run/docker.sock" "${PODMAN_LOG}"; then
  echo "FAIL: expected inner docker CLI to point at the mounted Podman socket" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-e EXPORTERS_FROM_IMAGE=1" "${PODMAN_LOG}"; then
  echo "FAIL: expected exporters to be seeded from the wrapper image" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if grep -q -- "--test-values" "${PODMAN_LOG}"; then
  echo "FAIL: BlockSci test values must require an explicit --test-values option" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "^container cleanup --rm isolated_docker_daemon " "${PODMAN_LOG}"; then
  echo "FAIL: expected Podman cleanup to clean and remove isolated_docker_daemon" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "^rm -f -i emulator_manager btc_data_wiper dind_image_prefetch isolated_docker_daemon " "${PODMAN_LOG}"; then
  echo "FAIL: expected Podman cleanup to remove recreate containers" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "^network rm -f blocksci-emulator_default " "${PODMAN_LOG}"; then
  echo "FAIL: expected Podman cleanup to remove the recreate network" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

KUBE_DIR="${TMP_DIR}/kube"
KUBE_CONFIG="${KUBE_DIR}/config"
mkdir -p "${KUBE_DIR}"
touch "${KUBE_CONFIG}"
: >"${PODMAN_LOG}"

(
  cd "${PROJECT_DIR}"
  CONTAINER_SOCKET="${SOCKET_PATH}" \
  EMULATION_LOGS_DIR="${FAKE_LOGS}" \
  KUBERNETES_CONTROL_IP="172.17.0.1" \
  WRAPPER_IMAGE="ghcr.io/ondrejman/coinjoin-pipeline:latest" \
  PATH="${FAKE_BIN}:${PATH}" \
  ./runIt.sh container podman recreate \
    --engine wasabi \
    --scenario overactive-local.json \
    --driver=kubernetes \
    --namespace=coinjoin-test \
    --kubeconfig="${KUBE_CONFIG}" \
    --reuse-namespace \
    --copy-to-host \
    --image-prefix ghcr.io/test/
)

if ! grep -q -- "ghcr.io/ondrejman/coinjoin-pipeline:latest" "${PODMAN_LOG}"; then
  echo "FAIL: expected WRAPPER_IMAGE override to select the wrapper image" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "--kubeconfig ${KUBE_CONFIG} get --raw=/version" "${KUBECTL_LOG}"; then
  echo "FAIL: expected doctor to probe the selected Kubernetes API" >&2
  echo "Observed: $(cat "${KUBECTL_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "-v ${KUBE_CONFIG}:${KUBE_CONFIG}:ro" "${PODMAN_LOG}"; then
  echo "FAIL: expected --kubeconfig= path to be mounted into the wrapper" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "--driver kubernetes" "${PODMAN_LOG}"; then
  echo "FAIL: expected Kubernetes driver to be forwarded to wrapper.py" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi
if ! grep -q -- "--copy-to-host" "${PODMAN_LOG}"; then
  echo "FAIL: expected --copy-to-host to be forwarded to wrapper.py" >&2
  exit 1
fi

if ! grep -q -- "-e KUBERNETES_CONTROL_IP=172.17.0.1" "${PODMAN_LOG}"; then
  echo "FAIL: expected Kubernetes control IP to be forwarded to wrapper container" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "--namespace coinjoin-test" "${PODMAN_LOG}"; then
  echo "FAIL: expected --namespace= value to be forwarded to wrapper.py" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "--reuse-namespace --copy-to-host --image-prefix ghcr.io/test/" "${PODMAN_LOG}"; then
  echo "FAIL: expected pass-through Kubernetes flags to remain in wrapper args" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

if grep -q -- "_ recreate .*--test-values" "${PODMAN_LOG}"; then
  echo "FAIL: expected recreate-only run not to receive BlockSci test values flag" >&2
  echo "Observed: $(cat "${PODMAN_LOG}")" >&2
  exit 1
fi

echo "PASS: container podman path does not use host docker and mounts Podman's socket for the wrapper."
