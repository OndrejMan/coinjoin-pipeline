#!/usr/bin/env bash
# Regression: ``podman compose`` runs docker-compose as a child and does not
# forward SIGTERM to it.  emulate.sh used to kill only the wrapper, so the
# orphaned ``logs -f`` follower kept stdout open and the caller hung forever.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_DIR="${REPO_ROOT}/pipeline"
TMP_DIR="$(mktemp -d)"
FOLLOWER_PID_FILE="${TMP_DIR}/follower.pid"

cleanup() {
  if [[ -s "${FOLLOWER_PID_FILE}" ]]; then
    kill "$(cat "${FOLLOWER_PID_FILE}")" >/dev/null 2>&1 || true
  fi
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

FAKE_BIN="${TMP_DIR}/bin"
mkdir -p "${FAKE_BIN}"

cat >"${FAKE_BIN}/podman" <<'FAKE'
#!/usr/bin/env bash
if [[ "$1" == "compose" && "$*" == *" ps -q manager"* ]]; then
  echo "manager-container-id"
elif [[ "$1" == "compose" && "$*" == *" logs -f"* ]]; then
  # Like podman's external compose provider: a child that outlives this wrapper.
  sleep 300 &
  echo "$!" >"${FOLLOWER_PID_FILE:?}"
  wait
elif [[ "$1" == "wait" ]]; then
  for _ in {1..50}; do
    [[ -s "${FOLLOWER_PID_FILE:?}" ]] && break
    sleep 0.1
  done
  echo "0"
fi
exit 0
FAKE
chmod +x "${FAKE_BIN}/podman"
export FOLLOWER_PID_FILE

set +e
START="${SECONDS}"
# Write to a file, not a pipe: an orphan holding a pipe would hang this test
# instead of failing it.
(
  cd "${PROJECT_DIR}"
  PATH="${FAKE_BIN}:${PATH}" \
    CONTAINER_RUNTIME=podman \
    COMPOSE_FILE="${PROJECT_DIR}/compose.yaml" \
    timeout 30 bash emulate.sh >"${TMP_DIR}/emulate.log" 2>&1
)
RUN_EXIT_CODE=$?
ELAPSED=$((SECONDS - START))
set -e

if [[ "${RUN_EXIT_CODE}" -ne 0 ]]; then
  echo "FAIL: expected emulate.sh to exit 0 without hanging, got ${RUN_EXIT_CODE} after ${ELAPSED}s" >&2
  cat "${TMP_DIR}/emulate.log" >&2
  exit 1
fi

if [[ ! -s "${FOLLOWER_PID_FILE}" ]]; then
  echo "FAIL: fake log follower never started" >&2
  exit 1
fi

if kill -0 "$(cat "${FOLLOWER_PID_FILE}")" >/dev/null 2>&1; then
  echo "FAIL: emulate.sh left the compose log follower's child running" >&2
  exit 1
fi

echo "PASS: emulate.sh stops the whole compose log follower and releases stdout (${ELAPSED}s)."
