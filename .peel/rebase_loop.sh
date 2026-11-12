#!/bin/bash
# Continue a rebase whose exec fixers are idempotent: every conflicted file takes the picked commit's side.
cd "$(dirname "$0")/.." || exit 1
for i in $(seq 1 60); do
  [ -d .git/rebase-merge ] || break
  git diff --name-only --diff-filter=U | while read -r f; do
    echo "resolve: $(head -1 .git/rebase-merge/message) :: $f"
    if git cat-file -e ":3:$f" 2>/dev/null; then
      git checkout --theirs -- "$f" && git add "$f"
    else
      git rm -q -- "$f"
    fi
  done
  GIT_EDITOR=true git rebase --continue 2>&1 | grep -E "^[a-z-]+: |CONFLICT|error:" | head -5
done
