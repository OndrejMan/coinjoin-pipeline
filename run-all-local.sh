#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Route the suite through the watch wrapper when it is available: every line
# timestamped and tee'd to emulation_logs/.watch/run-all-<ts>.log, the suite pid
# recorded for the watcher, measured per-test durations folded back into the
# skill's table, and a failure evidence bundle collected on a red exit.
# Set RUN_SUITE_DISABLE=1 to bypass it, or RUN_SUITE=<path> to point elsewhere.
RUN_SUITE="${RUN_SUITE:-${SCRIPT_DIR}/../.agents/skills/pipeline-test-suite-watch/scripts/run-suite.sh}"
if [[ -z "${RUN_SUITE_DISABLE:-}" && -x "${RUN_SUITE}" ]]; then
  exec "${RUN_SUITE}" local "$@"
fi

exec "${SCRIPT_DIR}/run-all.sh" local "$@"
