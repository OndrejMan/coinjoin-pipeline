#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${COINJOIN_COMPOSE_PROJECT:-blocksci-emulator}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${SCRIPT_DIR}/compose.yaml}"
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

# Profiled services are otherwise omitted by some Compose versions, leaving a
# stopped DinD container whose writable layer can retain stale runtime PID
# files. Activate both profiles while removing the complete project stack.
"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${PROJECT_NAME}" \
  --profile emulate --profile analysis down --remove-orphans
"${CONTAINER_RUNTIME}" volume rm "${PROJECT_NAME}_btc_data" "${PROJECT_NAME}_blocksci_cache" "${PROJECT_NAME}_emulation_logs" >/dev/null 2>&1 || true
