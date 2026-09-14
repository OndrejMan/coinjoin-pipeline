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

# Reap Compose projects abandoned by earlier runs.
#
# The project name carries the launching process's pid, so the teardown above
# can only ever address the project this run is about to create — on a fresh run
# that is a no-op, and no later run ever names an earlier project again. The
# leftovers cost nothing but a Docker subnet each, which is why they went
# unnoticed: 29 of them accumulated from 2026-08-26 on and exhausted the default
# address pools, failing a suite two hours in with "all predefined address pools
# have been fully subnetted".
#
# Only projects whose owning pid is gone are touched. Tests run back to back and
# --parallel starts several runs at once, so a live pid must never be reaped;
# /proc is consulted first because `kill -0` also fails for a live process owned
# by another user, which would read as "gone".
process_alive() {
  [[ -e "/proc/${1}" ]] || kill -0 "${1}" 2>/dev/null
}

reap_abandoned_projects() {
  local project pid
  # Volumes are listed too: once a project's network and containers are gone
  # (an earlier sweep, or this one before it removed volumes), the volumes are
  # the only trace left, and they are the part that holds the disk.
  for project in $(
    {
      "${CONTAINER_RUNTIME}" network ls --format '{{.Name}}' 2>/dev/null | sed -n 's/_default$//p'
      "${CONTAINER_RUNTIME}" ps -a --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null
      "${CONTAINER_RUNTIME}" volume ls --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null
    } | sort -u
  ); do
    [[ "${project}" =~ ^cjp-.+-([0-9]+)$ ]] || continue
    [[ "${project}" == "${PROJECT_NAME}" ]] && continue
    pid="${BASH_REMATCH[1]}"
    if process_alive "${pid}"; then
      continue
    fi
    echo "Reaping abandoned Compose project ${project} (pid ${pid} gone)"
    # Volumes go too. The teardown above keeps dind_cache so a project can reuse
    # its image cache — but a pid-scoped project never recurs, so its cache is
    # never read again and only occupies disk: 13 of them held ~11 GB each on
    # 2026-09-13. The other volumes are ones the teardown above already removes
    # for its own project, so nothing here is treated as more precious than the
    # current run treats its own data.
    "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" -p "${project}" \
      --profile emulate --profile analysis down --remove-orphans --volumes >/dev/null 2>&1 || true
  done
}

# Never let housekeeping fail the stage that asked for a clean slate.
reap_abandoned_projects || true
