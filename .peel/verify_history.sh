#!/bin/bash
# Run the peel gate on every commit BASE..REF (BASE defaults to target) in a throwaway worktree.
set -u
cd "$(dirname "$0")/.." || exit 1
ref=${1:-HEAD}
wt=$(mktemp -d /tmp/claude-1000/peel-verify-wt.XXXXXX); rmdir "$wt"
git worktree remove --force "$wt" 2>/dev/null
git worktree add -q --detach "$wt" "$ref" || exit 1
cp -r .peel "$wt/.peel"
ln -s "$PWD/.venv" "$wt/.venv"
cd "$wt" || exit 1
export TMPDIR=/tmp/claude-1000/peel-verify-tmp; mkdir -p "$TMPDIR"
fail=0
for c in $(git rev-list --reverse "${2:-target}".."$ref"); do
  git checkout -q --detach "$c"
  out=$(.peel/check.sh 2>&1)
  if [ $? -ne 0 ]; then
    fail=1
    echo "FAIL $(git log -1 --format='%h %s' "$c")"
    echo "$out" | head -8 | sed 's/^/    /'
  fi
done
cd - >/dev/null
git worktree remove --force "$wt"
[ $fail -eq 0 ] && echo "all commits pass"
