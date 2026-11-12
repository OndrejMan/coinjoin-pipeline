#!/bin/bash
# Repo paths named in tracked files that do not exist, minus those already stale at target.
# With an argument, check that commit instead of the working tree (and do not filter).
cd "$(dirname "$0")/.." || exit 1
re='(?<![A-Za-z0-9_./-])(src|pipeline|tests|scripts|container|examples|scenarios|docs|tools)/[A-Za-z0-9_./-]+\.(py|sh|yaml|yml|json|md|txt)'
ref=${1:-}
scan() {
  if [ -n "$ref" ]; then
    git ls-tree -r --name-only "$ref" | grep -vE '\.(ipynb|lock)$' | while read -r f; do
      git show "$ref:$f" 2>/dev/null | grep -oP "$re" | sort -u | while read -r p; do
        git cat-file -e "$ref:$p" 2>/dev/null || echo "$f -> $p"
      done
    done
  else
    git ls-files | grep -vE '\.(ipynb|lock)$' | while read -r f; do
      [ -f "$f" ] || continue
      grep -oP "$re" "$f" 2>/dev/null | sort -u | while read -r p; do [ -e "$p" ] || echo "$f -> $p"; done
    done
  fi
}
if [ -n "$ref" ]; then scan; else scan | grep -vxF -f .peel/stale-at-target.txt; fi
exit 0
