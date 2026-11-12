# shellcheck shell=bash
# Sourced by emulate.sh and analysis.sh: the Compose file and project, the container
# runtime and its Compose command. The CLI resolves the runtime and command in
# execution/runtime.py and exports them; these defaults serve a script run by hand.
COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/compose.yaml}"
PROJECT_NAME="${COINJOIN_COMPOSE_PROJECT:-blocksci-emulator}"
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
