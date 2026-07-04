#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_DIR}"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s tests \
  -p 'test_command_builder.py' \
  -v

WRAPPER_ROOT="${PROJECT_DIR}/pipeline"
if [[ ! -f "${WRAPPER_ROOT}/client/wrapper.py" || ! -f "${WRAPPER_ROOT}/client/research.py" ]]; then
  echo "FAIL: merged wrapper parser is missing." >&2
  exit 1
fi

PYTHONDONTWRITEBYTECODE=1 python3 scripts/generate-command-metadata.py \
  --check \
  --wrapper-root "${WRAPPER_ROOT}"

echo "PASS: command builder metadata matches the wrapper and research parsers."
