#!/usr/bin/env bash
# The in-process pipeline owns the peer container cleanup that the removed
# launcher's INT/TERM trap used to do.
#
# Runs the real `python -m coinjoin_pipeline.cli` invocation with only the
# container runtime stubbed, so it exercises what production runs. Both
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

# The IDs the stub reports for the Compose project's running containers.
PEERS="cid-manager cid-dind"

# A docker stub that blocks only while Compose starts the emulation, so the
# signal arrives while the wrapper is mid-stage. Teardown commands must return
# normally; the emulation script invokes `compose down` from its EXIT cleanup.
cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${DOCKER_LOG:?}"
printf '\n' >>"${DOCKER_LOG:?}"
if [[ "$1" == "compose" && " $* " == *" up -d "* ]]; then
  echo "$$" >"${STAGE_PID_FILE:?}"
  touch "${STAGE_STARTED:?}"
  while true; do sleep 1; done
fi
if [[ "$1" == "ps" && " $* " == *" label=com.docker.compose.project="* ]]; then
  printf 'cid-manager\ncid-dind\n'
  exit 0
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
  # Each case gets its own run ID: generated IDs have minute resolution, so the
  # second case would otherwise find the first one's launch and be refused.
  python3 - "${command_file}" "${LOGS_ROOT}" "signal-${signal,,}" <<'PY'
import sys
from pathlib import Path

import shlex

destination, runs_root, run_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
command = [sys.executable, "-m", "coinjoin_pipeline.cli", "emulate", "--engine", "wasabi",
           "--scenario", "overactive-local.json", "--runs-root", str(runs_root), "--run-id", run_id]
destination.write_text("exec " + shlex.join(command) + "\n", encoding="utf-8")
PY

  (
    cd "${PROJECT_DIR}"
    export DOCKER_LOG="${docker_log}"
    export STAGE_STARTED="${stage_started}"
    export STAGE_PID_FILE="${stage_pid_file}"
    export PATH="${FAKE_BIN}:${PATH}"
    # Same import path as the runIt.sh fallback: the package and its exporters.
    export PYTHONPATH="${PROJECT_DIR}/src:${PROJECT_DIR}/pipeline${PYTHONPATH:+:${PYTHONPATH}}"
    # Replace this asynchronous subshell with the wrapper. Otherwise `$!`
    # identifies the signal-ignoring background shell rather than the
    # wrapper process that owns the cleanup handlers.
    exec bash "${command_file}"
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

  # `wait` has no built-in timeout. Keep a broken signal path from freezing
  # the complete local test suite and retain the wrapper diagnostics below.
  local timeout_marker="${TMP_DIR}/wrapper-${signal}.timed-out"
  (
    sleep 5
    if kill -0 "${wrapper_pid}" >/dev/null 2>&1; then
      : >"${timeout_marker}"
      kill -TERM "${wrapper_pid}" >/dev/null 2>&1 || true
    fi
  ) &
  local watchdog_pid=$!
  set +e
  wait "${wrapper_pid}"
  local exit_code=$?
  kill "${watchdog_pid}" >/dev/null 2>&1 || true
  wait "${watchdog_pid}" >/dev/null 2>&1 || true
  set -e

  if [[ -e "${timeout_marker}" ]]; then
    echo "FAIL: [${signal}] wrapper did not exit within 5 seconds of the signal" >&2
    echo "Wrapper output:" >&2
    cat "${output}" >&2
    echo "Docker calls:" >&2
    cat "${docker_log}" >&2
    exit 1
  fi

  if [[ "${exit_code}" -ne 130 ]]; then
    echo "FAIL: [${signal}] expected exit 130, got ${exit_code}" >&2
    echo "Observed: $(cat "${output}")" >&2
    exit 1
  fi

  # The cleanup must look up the very project the emulation was started under.
  local project
  project="$(sed -n 's/^compose .*-p \([^ ]*\) .* up -d .*/\1/p' "${docker_log}" | head -n 1)"
  if [[ -z "${project}" ]] || ! grep -q "^ps -q --filter label=com.docker.compose.project=${project} " "${docker_log}"; then
    echo "FAIL: [${signal}] the wrapper did not look up the containers of Compose project '${project}'" >&2
    echo "Observed: $(cat "${docker_log}")" >&2
    exit 1
  fi

  local missing
  for missing in ${PEERS}; do
    if ! grep -q "^stop .*${missing}" "${docker_log}"; then
      echo "FAIL: [${signal}] container ${missing} of the Compose project was not stopped" >&2
      echo "Observed: $(cat "${docker_log}")" >&2
      exit 1
    fi
  done

  echo "  [${signal}] exit 130 and the Compose project's containers stopped."
}

echo "Checking bare-wrapper signal cleanup..."
run_signal_case INT
run_signal_case TERM

echo "PASS: the bare wrapper stops its Compose project's containers on SIGINT and SIGTERM."
