#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

ISOLATED_ROOT="${TMP_DIR}/repo"
ISOLATED_PROJECT="${ISOLATED_ROOT}/coinjoin-pipeline"
FAKE_BIN="${TMP_DIR}/bin"
DOCKER_LOG="${TMP_DIR}/docker.args"
mkdir -p \
  "${ISOLATED_PROJECT}" \
  "${ISOLATED_ROOT}/blocksci" \
  "${ISOLATED_ROOT}/coinjoin-emulator" \
  "${ISOLATED_ROOT}/coinjoin-analysis/docker" \
  "${FAKE_BIN}"
cp "${PROJECT_DIR}/run-all.sh" "${ISOLATED_PROJECT}/run-all.sh"

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${DOCKER_LOG:?}"
printf '\n' >>"${DOCKER_LOG:?}"
exit 0
EOF
chmod +x "${FAKE_BIN}/docker"

export DOCKER_LOG
(
  cd "${ISOLATED_PROJECT}"
  PATH="${FAKE_BIN}:${PATH}" bash run-all.sh local --build-only
)

if ! grep -q -- "build -t blocksci-cj:local -f ${ISOLATED_ROOT}/blocksci/Dockerfile ${ISOLATED_ROOT}/blocksci " "${DOCKER_LOG}"; then
  echo "FAIL: local sweep did not build the BlockSci dependency image" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "build --build-arg BLOCKSCI_BASE_IMAGE=blocksci-cj:local --build-arg NTHREADS=10 -t blocksci-complete:local -f ${ISOLATED_ROOT}/blocksci/Dockerfile_complete ${ISOLATED_ROOT}/blocksci " "${DOCKER_LOG}"; then
  echo "FAIL: local sweep did not build blocksci-complete from Dockerfile_complete" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "run --rm --entrypoint /bin/bash blocksci-complete:local -lc " "${DOCKER_LOG}"; then
  echo "FAIL: local sweep did not smoke-check the complete BlockSci image" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

echo "PASS: run-all.sh builds and verifies the complete local BlockSci image."
