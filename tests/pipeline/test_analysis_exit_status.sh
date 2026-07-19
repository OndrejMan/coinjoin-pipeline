#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_DIR="${REPO_ROOT}/pipeline"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

FAKE_BIN="${TMP_DIR}/bin"
DOCKER_LOG="${TMP_DIR}/docker.args"
mkdir -p "${FAKE_BIN}"

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${DOCKER_LOG:?}"
printf '\n' >>"${DOCKER_LOG:?}"

if [[ "$1" == "compose" && "$*" == *" ps -a -q blocksci"* ]]; then
  echo "blocksci-container-id"
elif [[ "$1" == "inspect" ]]; then
  echo "127"
fi
exit 0
EOF
chmod +x "${FAKE_BIN}/docker"

export DOCKER_LOG

set +e
(
  cd "${PROJECT_DIR}"
  PATH="${FAKE_BIN}:${PATH}" \
  ACTIVE_RUN_ID="analysis-exit-status-test" \
  HOST_CLIENT_DIR="${PROJECT_DIR}/client" \
  COMPOSE_FILE="${PROJECT_DIR}/compose.yaml" \
  bash analysis.sh
)
RUN_EXIT_CODE=$?
set -e

if [[ "${RUN_EXIT_CODE}" -ne 127 ]]; then
  echo "FAIL: expected analysis.sh to propagate BlockSci exit 127, got ${RUN_EXIT_CODE}" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

echo "PASS: analysis.sh propagates the BlockSci container exit status."
