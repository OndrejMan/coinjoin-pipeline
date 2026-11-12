#!/bin/sh
# ruff format --check over working-tree .py files that are format-clean at target.
cd "$(dirname "$0")/.." || exit 1
git ls-files '*.py' | grep -vxF -f .peel/unformatted-at-target.txt | xargs .venv/bin/ruff format --check 2>&1 | grep "Would reformat" || true
