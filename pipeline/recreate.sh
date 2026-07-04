#!/usr/bin/env bash
set -euo pipefail

COINJOIN_ENGINE="${COINJOIN_ENGINE:-wasabi}"
if [[ "${COINJOIN_ENGINE}" == "joinmarket" ]]; then
  SCENARIO_PATH="/app/scenarios/defaultJoinMarket.json"
else
  SCENARIO_PATH="/app/scenarios/overactive-local.json"
fi
COMPOSE_FILE="${COMPOSE_FILE:-}"
PROJECT_NAME="blocksci-emulator"
if [[ -z "${HOST_CLIENT_DIR:-}" ]]; then
  HOST_CLIENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/client" && pwd)"
fi
export SCENARIOS_DIR="${HOST_CLIENT_DIR}/scenarios"
HOST_ROOT_DIR="$(dirname "${HOST_CLIENT_DIR}")"
export EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR:-${HOST_ROOT_DIR}/emulation_logs}"
if [[ -z "${COMPOSE_FILE}" ]]; then
  COMPOSE_FILE="${HOST_ROOT_DIR}/compose.yaml"
fi
if [[ ! -f "${COMPOSE_FILE}" && -f /compose.yaml ]]; then
  COMPOSE_FILE="/compose.yaml"
fi

CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"
if [[ "${CONTAINER_RUNTIME}" != "docker" && "${CONTAINER_RUNTIME}" != "podman" ]]; then
  echo "Unsupported CONTAINER_RUNTIME='${CONTAINER_RUNTIME}' (expected docker or podman)" >&2
  exit 2
fi

if [[ -n "${CONTAINER_COMPOSE_COMMAND:-}" ]]; then
  read -r -a COMPOSE_CMD <<< "${CONTAINER_COMPOSE_COMMAND}"
else
  COMPOSE_CMD=("${CONTAINER_RUNTIME}" "compose")
fi

LOG_PID=""
WAIT_PID=""
STACK_STARTED=false

cleanup_recreate_stack() {
  if [[ -n "${WAIT_PID}" ]] && kill -0 "${WAIT_PID}" >/dev/null 2>&1; then
    kill "${WAIT_PID}" >/dev/null 2>&1 || true
    wait "${WAIT_PID}" >/dev/null 2>&1 || true
  fi

  if [[ -n "${LOG_PID}" ]] && kill -0 "${LOG_PID}" >/dev/null 2>&1; then
    kill "${LOG_PID}" >/dev/null 2>&1 || true
    wait "${LOG_PID}" >/dev/null 2>&1 || true
  fi

  if [[ "${STACK_STARTED}" == "true" ]]; then
    if ! "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile recreate down; then
      echo "WARN: compose down failed during cleanup" >&2
    fi
  fi
}

handle_interrupt() {
  trap - INT TERM
  echo "Interrupted; stopping recreate stack..." >&2
  exit 130
}

trap handle_interrupt INT TERM
trap 'status=$?; cleanup_recreate_stack; exit "${status}"' EXIT

if [[ $# -gt 0 ]]; then
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
    SCENARIO_TRIMMED="${SCENARIO_TRIMMED#client/scenarios/}"
    SCENARIO_TRIMMED="${SCENARIO_TRIMMED#scenarios/}"
    SCENARIO_PATH="/app/scenarios/${SCENARIO_TRIMMED}"
  fi
fi

echo "Using scenario: ${SCENARIO_PATH}"
echo "Using engine: ${COINJOIN_ENGINE}"

# 1. Start the stack in the background
SCENARIO_PATH="${SCENARIO_PATH}" COINJOIN_ENGINE="${COINJOIN_ENGINE}" "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile recreate up -d
STACK_STARTED=true

# 2. Start streaming logs in the background 
# The `-f` flag "follows" the logs in real-time.
# The `&` at the end runs this command in the background so the script can move on.
"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile recreate logs -f &

# Capture the Process ID (PID) of that background log stream
LOG_PID=$!

# 3. Wait ONLY for the manager to exit and capture its exit code
MANAGER_CONTAINER_ID=$("${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" --profile recreate ps -q manager)
"${CONTAINER_RUNTIME}" wait "${MANAGER_CONTAINER_ID}" &
WAIT_PID=$!
set +e
wait "${WAIT_PID}"
EXIT_CODE=$?
set -e
WAIT_PID=""

# Exit your terminal or CI/CD pipeline with the manager's exit code.
# The EXIT trap tears the compose stack down while preserving this status.
exit "${EXIT_CODE}"
