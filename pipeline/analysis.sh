#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${HOST_CLIENT_DIR:-}" ]]; then
	HOST_CLIENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/client" && pwd)"
fi

export SCENARIOS_DIR="${HOST_CLIENT_DIR}/scenarios"
export NOTEBOOKS_DIR="${HOST_CLIENT_DIR}/notebooks"
HOST_ROOT_DIR="$(dirname "${HOST_CLIENT_DIR}")"
export EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR:-${HOST_ROOT_DIR}/emulation_logs}"
export EXPORTERS_DIR="${EXPORTERS_DIR:-${HOST_ROOT_DIR}/exporters}"
if [[ -n "${ACTIVE_RUN_ID:-}" ]]; then
	export COINJOIN_ANALYSIS_SOURCE_PATH="${COINJOIN_ANALYSIS_SOURCE_PATH:-${EMULATION_LOGS_DIR}/${ACTIVE_RUN_ID}/coinjoin-analysis_data}"
	export COINJOIN_ANALYSIS_MOUNT_PATH="${COINJOIN_ANALYSIS_MOUNT_PATH:-/runs/emulation/selected/${ACTIVE_RUN_ID}}"
	export COINJOIN_ANALYSIS_TARGET_PATH="${COINJOIN_ANALYSIS_TARGET_PATH:-/runs/emulation/selected}"
	export COINJOIN_ANALYSIS_INPUT_DATA_PATH="${COINJOIN_ANALYSIS_INPUT_DATA_PATH:-${EMULATION_LOGS_DIR}/${ACTIVE_RUN_ID}/coinjoin_emulator_data/data}"
	mkdir -p "${COINJOIN_ANALYSIS_SOURCE_PATH}"
else
	echo "ACTIVE_RUN_ID is required for grouped artifacts." >&2
	exit 2
fi
COMPOSE_FILE="${COMPOSE_FILE:-${HOST_ROOT_DIR}/compose.yaml}"
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

"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p blocksci-emulator --profile analysis rm -sf blocksci coinjoin_analysis
"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p blocksci-emulator --profile analysis up --build --force-recreate
