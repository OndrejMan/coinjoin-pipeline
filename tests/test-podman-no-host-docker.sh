#!/usr/bin/env bash
# The Podman path must never touch a host Docker daemon. With the wrapper
# container gone there is no socket to forward any more, so what remains to
# guarantee is simply: selecting podman uses podman, and `docker` is never run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

FAKE_BIN="${TMP_DIR}/bin"
FAKE_LOGS="${TMP_DIR}/logs"
PODMAN_LOG="${TMP_DIR}/podman.args"
DOCKER_LOG="${TMP_DIR}/docker.called"
KUBECTL_LOG="${TMP_DIR}/kubectl.args"
RENDERED="${TMP_DIR}/rendered.txt"
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
export BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-ghcr.io/ondrejman/blocksci-complete:latest}"
export COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}"
export COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"

fail_with() {
  echo "FAIL: $1" >&2
  echo "Observed: $(cat "${RENDERED}")" >&2
  exit 1
}

# The rendered command is printed before the preflight runs, so grepping it
# proves nothing on its own: a failed run would leave every assertion below
# satisfied. Every invocation therefore has to be checked for its exit code too.
run_pipeline() {
  local status=0
  (
    cd "${PROJECT_DIR}"
    EMULATION_LOGS_DIR="${FAKE_LOGS}" \
    KUBERNETES_CONTROL_IP="172.17.0.1" \
    PATH="${FAKE_BIN}:${PATH}" \
    ./runIt.sh "$@"
  ) >"${RENDERED}" 2>&1 || status=$?
  if [[ "${status}" -ne 0 ]]; then
    fail_with "expected 'runIt.sh $*' to succeed, got exit ${status}"
  fi
}

# --- local Podman run --------------------------------------------------------
run_pipeline container podman emulate --engine wasabi \
  --scenario overactive-local.json --dry-run

if [[ -e "${DOCKER_LOG}" ]]; then
  echo "FAIL: the Podman path must never invoke the host docker binary" >&2
  exit 1
fi

# The preflight is the only part that reaches a runtime on a dry run; it must
# reach podman, and only podman.
grep -q '^info ' "${PODMAN_LOG}" \
  || fail_with "expected the preflight to probe podman, not another runtime"
grep -q '^image inspect ' "${PODMAN_LOG}" \
  || fail_with "expected the emulator image preflight to run through podman"

grep -Fq -- "CONTAINER_RUNTIME=podman" "${RENDERED}" \
  || fail_with "expected the selected runtime to reach the wrapper"

grep -Fq -- "pipeline/client/wrapper.py" "${RENDERED}" \
  || fail_with "expected the wrapper to run bare from the checkout"

# Socket forwarding only ever existed to reach the host daemon from inside the
# wrapper container; running bare there is nothing to mount or point at.
if grep -Fq -- "/var/run/docker.sock" "${RENDERED}"; then
  fail_with "a bare wrapper must not mount or forward a container socket"
fi
if grep -Fq -- "DOCKER_HOST=" "${RENDERED}"; then
  fail_with "a bare wrapper must not rewrite DOCKER_HOST"
fi
if grep -Fq -- "--pull" "${RENDERED}"; then
  fail_with "wrapper-image pull policy flags no longer exist"
fi
if grep -Fq -- "--test-values" "${RENDERED}"; then
  fail_with "Removed BlockSci test-values option was rendered"
fi

# --- Kubernetes driver still probes the selected API -------------------------
KUBE_DIR="${TMP_DIR}/kube"
KUBE_CONFIG="${KUBE_DIR}/config"
mkdir -p "${KUBE_DIR}"
touch "${KUBE_CONFIG}"
: >"${PODMAN_LOG}"

run_pipeline container podman emulate \
  --engine wasabi \
  --scenario overactive-local.json \
  --driver=kubernetes \
  --namespace=coinjoin-test \
  --kubeconfig="${KUBE_CONFIG}" \
  --reuse-namespace \
  --image-prefix ghcr.io/test/ \
  --dry-run

if [[ -e "${DOCKER_LOG}" ]]; then
  echo "FAIL: the Kubernetes Podman path must never invoke host docker either" >&2
  exit 1
fi

grep -q '^info ' "${PODMAN_LOG}" \
  || fail_with "expected the Kubernetes preflight to probe podman as well"

grep -Fq -- "--kubeconfig=${KUBE_CONFIG}" "${RENDERED}" \
  || fail_with "expected the selected kubeconfig to be forwarded"
grep -Fq -- "KUBERNETES_CONTROL_IP=172.17.0.1" "${RENDERED}" \
  || fail_with "expected the Kubernetes control IP to reach the wrapper environment"

echo "PASS: the Podman path runs bare and never reaches for a host Docker daemon."
