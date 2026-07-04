#!/usr/bin/env bash
# Legacy path retained for callers that used container/launcher.sh directly.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v coinjoin-pipeline >/dev/null 2>&1; then
  exec coinjoin-pipeline "$@"
fi
export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m coinjoin_pipeline.cli "$@"
