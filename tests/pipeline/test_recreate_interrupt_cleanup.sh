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
WAIT_STARTED="${TMP_DIR}/wait.started"
mkdir -p "${FAKE_BIN}"

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${DOCKER_LOG:?}"
printf '\n' >>"${DOCKER_LOG:?}"

if [[ "$1" == "compose" && "$*" == *" ps -q manager"* ]]; then
  echo "manager-container-id"
  exit 0
fi

if [[ "$1" == "compose" && "$*" == *" logs -f"* ]]; then
  while true; do sleep 1; done
fi

if [[ "$1" == "wait" ]]; then
  touch "${WAIT_STARTED:?}"
  while true; do sleep 1; done
fi

exit 0
EOF
chmod +x "${FAKE_BIN}/docker"

export DOCKER_LOG WAIT_STARTED

(
  cd "${PROJECT_DIR}"
  PATH="${FAKE_BIN}:${PATH}" \
  COMPOSE_FILE="${PROJECT_DIR}/compose.yaml" \
  bash recreate.sh
) &
RUN_PID=$!

for _ in {1..50}; do
  [[ -e "${WAIT_STARTED}" ]] && break
  sleep 0.1
done

if [[ ! -e "${WAIT_STARTED}" ]]; then
  echo "FAIL: recreate.sh did not reach docker wait" >&2
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
  echo "FAIL: expected recreate.sh to exit 130 after TERM, got ${RUN_EXIT_CODE}" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "compose -f ${PROJECT_DIR}/compose.yaml -p blocksci-emulator --profile recreate down " "${DOCKER_LOG}"; then
  echo "FAIL: expected recreate.sh interrupt cleanup to run compose down" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

echo "PASS: recreate.sh cleans up the compose stack on interrupt."
