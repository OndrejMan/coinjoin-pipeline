#!/usr/bin/env bash
# Drop every import ruff reports as unused (F401) from the given files.
set -euo pipefail
cd "$(dirname "$0")/.."
for f in "$@"; do
  mapfile -t names < <(.venv/bin/ruff check --no-cache --select F401 --output-format concise "$f" | grep ' F401 ' | sed -E 's/.*`([^`]+)`.*/\1/' | awk -F. '{print $NF}')
  [ ${#names[@]} -eq 0 ] && continue
  .venv/bin/python .peel/peel.py import "$f" "${names[@]}"
  echo "$f: removed ${names[*]}"
done
