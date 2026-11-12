#!/usr/bin/env bash
# EXPERIMENTAL: local Docker path. Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
set -euo pipefail

COINJOIN_ENGINE="${COINJOIN_ENGINE:-wasabi}"
HOST_ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SCENARIOS_DIR="${SCENARIOS_DIR:-${HOST_ROOT_DIR}/../scenarios}"
# The CLI's default runs root (paths.default_runs_root); the CLI always passes its own.
export EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR:-${PWD}/coinjoin-runs}"
export NOTEBOOKS_DIR="${NOTEBOOKS_DIR:-${EMULATION_LOGS_DIR}/.notebooks}"
# shellcheck source=pipeline/compose-runtime.sh
source "${HOST_ROOT_DIR}/compose-runtime.sh"

LOG_PID=""
LOG_PID_IS_GROUP=false
WAIT_PID=""
WAIT_RESULT_FILE="$(mktemp)"
STACK_STARTED=false

cleanup_emulate_stack() {
  if [[ -n "${WAIT_PID}" ]] && kill -0 "${WAIT_PID}" >/dev/null 2>&1; then
    kill "${WAIT_PID}" >/dev/null 2>&1 || true
    wait "${WAIT_PID}" >/dev/null 2>&1 || true
  fi

  if [[ -n "${LOG_PID}" ]]; then
    if [[ "${LOG_PID_IS_GROUP}" == "true" ]]; then
      kill -- "-${LOG_PID}" >/dev/null 2>&1 || true
    else
      kill "${LOG_PID}" >/dev/null 2>&1 || true
    fi
    wait "${LOG_PID}" >/dev/null 2>&1 || true
  fi

  if [[ "${STACK_STARTED}" == "true" ]]; then
    if ! "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile emulate down; then
      echo "WARN: compose down failed during cleanup" >&2
    fi
  fi

  rm -f "${WAIT_RESULT_FILE}"
}

handle_interrupt() {
  trap - INT TERM
  echo "Interrupted; stopping emulate stack..." >&2
  exit 130
}

trap handle_interrupt INT TERM
trap 'status=$?; cleanup_emulate_stack; exit "${status}"' EXIT

# The CLI always names the scenario; its default per engine lives in execution/settings.py.
if [[ $# -eq 0 ]]; then
  echo "emulate.sh needs a scenario: emulate.sh --scenario <file under scenarios/>" >&2
  exit 2
fi
if [[ "$1" == "--scenario" && $# -ge 2 ]]; then
  SCENARIO_INPUT="$2"
  shift 2
else
  SCENARIO_INPUT="$1"
  shift
fi

if [[ "$SCENARIO_INPUT" == /app/scenarios/* ]]; then
  SCENARIO_PATH="$SCENARIO_INPUT"
else
  SCENARIO_TRIMMED="${SCENARIO_INPUT#./}"
  SCENARIO_TRIMMED="${SCENARIO_TRIMMED#scenarios/}"
  SCENARIO_PATH="/app/scenarios/${SCENARIO_TRIMMED}"
fi

echo "Using scenario: ${SCENARIO_PATH}"
echo "Using engine: ${COINJOIN_ENGINE}"

# 1. Start the stack in the background
STACK_STARTED=true
SCENARIO_PATH="${SCENARIO_PATH}" COINJOIN_ENGINE="${COINJOIN_ENGINE}" "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile emulate up -d

# 2. Stream the logs in the background.
# ``podman compose`` runs an external provider (docker-compose) as a child and
# does not forward SIGTERM to it; killing only the wrapper orphaned a follower
# that kept this script's stdout pipe open and hung the caller after the stage
# finished.  Run the follower in its own process group and signal the group.
if command -v setsid >/dev/null 2>&1; then
  setsid "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile emulate logs -f &
  LOG_PID_IS_GROUP=true
else
  "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile emulate logs -f &
fi

LOG_PID=$!

# 3. Wait ONLY for the manager to exit and capture its exit code
MANAGER_CONTAINER_ID=$("${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile emulate ps -q manager)
"${CONTAINER_RUNTIME}" wait "${MANAGER_CONTAINER_ID}" >"${WAIT_RESULT_FILE}" &
WAIT_PID=$!
set +e
wait "${WAIT_PID}"
WAIT_COMMAND_EXIT_CODE=$?
set -e
WAIT_PID=""

if [[ "${WAIT_COMMAND_EXIT_CODE}" -ne 0 ]]; then
  echo "ERROR: ${CONTAINER_RUNTIME} wait failed with code ${WAIT_COMMAND_EXIT_CODE}" >&2
  exit "${WAIT_COMMAND_EXIT_CODE}"
fi

read -r EXIT_CODE <"${WAIT_RESULT_FILE}" || EXIT_CODE=""
if [[ ! "${EXIT_CODE}" =~ ^[0-9]+$ || "${EXIT_CODE}" -gt 255 ]]; then
  echo "ERROR: invalid manager exit code from ${CONTAINER_RUNTIME} wait: ${EXIT_CODE:-<empty>}" >&2
  exit 1
fi

# ``compose logs -f`` is terminated by the EXIT trap below.  Docker can still
# have the manager's final stderr frames in flight when ``docker wait``
# returns, so print a direct tail before teardown on failure.  Without this,
# the pipeline only reports the wrapper's generic CalledProcessError and loses
# the actual emulator exception that explains a non-zero manager exit.
if [[ "${EXIT_CODE}" -ne 0 ]]; then
  echo "ERROR: emulator manager exited with code ${EXIT_CODE}; final manager logs follow" >&2
  "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile emulate \
    logs --no-color --tail 200 manager >&2 || true
fi

# The manager's exit code; the EXIT trap tears the compose stack down and keeps it.
exit "${EXIT_CODE}"
