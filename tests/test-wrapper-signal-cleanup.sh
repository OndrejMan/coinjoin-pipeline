#!/usr/bin/env bash
# Hard gate from the wrapper-removal plan: the bare wrapper owns the peer
# container cleanup that the deleted launcher's INT/TERM trap used to do.
#
# Runs the real runtime command built by cli.py -- not `cjp` with a stubbed
# launcher -- so it exercises exactly the invocation production uses. Both
# signals are covered: SIGTERM never unwinds through atexit, so it used to
# leave the lock file behind as well.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

FAKE_BIN="${TMP_DIR}/bin"
LOGS_ROOT="${TMP_DIR}/logs"
mkdir -p "${FAKE_BIN}" "${LOGS_ROOT}"

PEERS="blocksci_analyzer coinjoin_analysis emulator_manager btc_data_wiper dind_image_prefetch isolated_docker_daemon"

# A docker stub that blocks on `compose` the way a real emulation would, so the
# signal arrives while the wrapper is mid-stage.
cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${DOCKER_LOG:?}"
printf '\n' >>"${DOCKER_LOG:?}"
if [[ "$1" == "compose" ]]; then
  echo "$$" >"${STAGE_PID_FILE:?}"
  touch "${STAGE_STARTED:?}"
  while true; do sleep 1; done
fi
# A real `docker stop` terminates the containers, which is what unblocks the
# compose call the wrapper is waiting on. Mirror that, or the wrapper hangs on
# a child that never dies and the cleanup can never be observed.
if [[ "$1" == "stop" && -s "${STAGE_PID_FILE:?}" ]]; then
  kill "$(cat "${STAGE_PID_FILE}")" >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod +x "${FAKE_BIN}/docker"

run_signal_case() {
  local signal="$1"
  local docker_log="${TMP_DIR}/docker-${signal}.args"
  local stage_started="${TMP_DIR}/stage-${signal}.started"
  local stage_pid_file="${TMP_DIR}/stage-${signal}.pid"
  : >"${stage_pid_file}"
  local output="${TMP_DIR}/wrapper-${signal}.out"
  : >"${docker_log}"

  # Build the invocation from the same function the production path uses.
  local command_file="${TMP_DIR}/command-${signal}.sh"
  PYTHONPATH="${PROJECT_DIR}/src" python3 - "${command_file}" "${LOGS_ROOT}" <<'PY'
import sys
from pathlib import Path

from coinjoin_pipeline.cli import runtime_root
from coinjoin_pipeline.commands import runtime_command
from coinjoin_pipeline.images import resolve_images

destination, runs_root = Path(sys.argv[1]), Path(sys.argv[2])
command = runtime_command(
    runtime_root(),
    "docker",
    ["emulate", "--engine", "wasabi", "--scenario", "overactive-local.json"],
    resolve_images(None, {}),
    runs_root,
    "coinjoin-pipeline emulate --engine wasabi",
)
# `exec` cannot take bare VAR=value prefixes; env applies them and keeps
# the wrapper as the signalled process rather than a bash child.
destination.write_text(f"exec env {command.rendered()}\n", encoding="utf-8")
PY

  (
    cd "${PROJECT_DIR}"
    DOCKER_LOG="${docker_log}" \
    STAGE_STARTED="${stage_started}" \
    STAGE_PID_FILE="${stage_pid_file}" \
    PATH="${FAKE_BIN}:${PATH}" \
    bash "${command_file}"
  ) >"${output}" 2>&1 &
  local wrapper_pid=$!

  local waited=0
  while [[ ! -e "${stage_started}" ]]; do
    sleep 0.2
    waited=$((waited + 1))
    if [[ "${waited}" -gt 150 ]]; then
      echo "FAIL: [${signal}] the wrapper never reached a peer-container stage" >&2
      echo "Observed: $(cat "${output}")" >&2
      kill -KILL "${wrapper_pid}" >/dev/null 2>&1 || true
      exit 1
    fi
  done

  kill "-${signal}" "${wrapper_pid}"
  set +e
  wait "${wrapper_pid}"
  local exit_code=$?
  set -e

  if [[ "${exit_code}" -ne 130 ]]; then
    echo "FAIL: [${signal}] expected exit 130, got ${exit_code}" >&2
    echo "Observed: $(cat "${output}")" >&2
    exit 1
  fi

  if ! grep -q "^stop .*blocksci_analyzer" "${docker_log}"; then
    echo "FAIL: [${signal}] the wrapper did not stop its peer containers" >&2
    echo "Observed: $(cat "${docker_log}")" >&2
    exit 1
  fi

  local missing
  for missing in ${PEERS}; do
    if ! grep -q "^stop .*${missing}" "${docker_log}"; then
      echo "FAIL: [${signal}] peer container ${missing} was not stopped" >&2
      echo "Observed: $(cat "${docker_log}")" >&2
      exit 1
    fi
  done

  echo "  [${signal}] exit 130 and all six peer containers stopped."
}

echo "Checking bare-wrapper signal cleanup..."
run_signal_case INT
run_signal_case TERM

echo "PASS: the bare wrapper cleans up peer containers on SIGINT and SIGTERM."
