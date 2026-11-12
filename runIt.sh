#!/usr/bin/env bash
# The installed CLI, else this checkout's sources (scenario-lab's direct launcher runs it so).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v coinjoin-pipeline >/dev/null 2>&1; then
  exec coinjoin-pipeline "$@"
fi
export PYTHONPATH="${SCRIPT_DIR}/src:${SCRIPT_DIR}/pipeline${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m coinjoin_pipeline.cli "$@"
