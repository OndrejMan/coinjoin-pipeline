#!/usr/bin/env bash
# Compatibility shim; orchestration lives in coinjoin_pipeline.pipeline_image.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v coinjoin-pipeline-image >/dev/null 2>&1; then
  exec coinjoin-pipeline-image "$@"
fi
export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m coinjoin_pipeline.pipeline_image "$@"
