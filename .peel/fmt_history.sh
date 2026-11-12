#!/bin/bash
# For each peel commit, list changed .py files (format-clean at target) that ruff would reformat.
cd "$(dirname "$0")/.." || exit 1
for c in $(git rev-list --reverse target..HEAD); do
  for f in $(git diff-tree --no-commit-id --name-only -r --diff-filter=AM "$c" -- '*.py'); do
    grep -qxF "$f" .peel/unformatted-at-target.txt && continue
    if ! git show "$c:$f" | .venv/bin/ruff format --check --stdin-filename "$f" - >/dev/null 2>&1; then
      echo "$(git log -1 --format='%h %s' "$c") :: $f"
    fi
  done
done
