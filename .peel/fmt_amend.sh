#!/bin/bash
# Run by rebase --exec: format the .py files HEAD touched (if clean at target) and amend.
cd "$(dirname "$0")/.." || exit 1
files=()
for f in $(git diff-tree --no-commit-id --name-only -r --diff-filter=AM HEAD -- '*.py'); do
  grep -qxF "$f" .peel/unformatted-at-target.txt || files+=("$f")
done
[ ${#files[@]} -eq 0 ] && exit 0
.venv/bin/ruff format -q "${files[@]}"
if ! git diff --quiet; then
  git commit -qa --amend --no-edit
  echo "formatted: $(git log -1 --format=%s) :: $(git diff-tree --no-commit-id --name-only -r HEAD@{1} HEAD | tr '\n' ' ')"
fi
