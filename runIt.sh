#!/usr/bin/env bash
# Backward-compatible entrypoint for the installed Python CLI.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v coinjoin-pipeline >/dev/null 2>&1; then
  exec coinjoin-pipeline "$@"
fi
export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m coinjoin_pipeline.cli "$@"
