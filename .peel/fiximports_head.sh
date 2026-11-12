#!/bin/bash
# rebase --exec helper: drop imports that became unused in the .py files HEAD touched, then amend.
cd "$(dirname "$0")/.." || exit 1
mapfile -t files < <(git diff-tree --no-commit-id --name-only -r --diff-filter=AM HEAD -- '*.py')
[ ${#files[@]} -eq 0 ] && exit 0
.peel/fiximports.sh "${files[@]}" >/dev/null
if ! git diff --quiet HEAD; then
  git commit -qa --amend --no-edit
  echo "imports: $(git log -1 --format=%s)"
fi
