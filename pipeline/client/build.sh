#!/usr/bin/env bash
set -euo pipefail

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

"${COMPOSE_CMD[@]}" run --build --remove-orphans cli
