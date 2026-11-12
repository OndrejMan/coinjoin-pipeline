#!/usr/bin/env bash
# Build the forward history from the peel branch: forward commit k gets the tree left after
# peel commit m-k and the message of peel commit m-k+1, so the last commit has the target tree.
set -euo pipefail
cd "$(dirname "$0")/.."
base="${1:-target}" peel="${2:-peel}" out="${3:-clean-history}"
mapfile -t P < <(git rev-list --reverse "$base..$peel")
m=${#P[@]}
[ "$m" -gt 0 ] || { echo "no peel commits" >&2; exit 1; }
trees=("$(git rev-parse "$base^{tree}")")
for c in "${P[@]}"; do trees+=("$(git rev-parse "$c^{tree}")"); done
empty="$(git hash-object -t tree /dev/null)"
[ "${trees[$m]}" = "$empty" ] || echo "WARNING: last peel tree is not empty (${trees[$m]})" >&2
now=$(date +%s)
parent=""
for ((k = 1; k <= m; k++)); do
  tree="${trees[$((m - k))]}"
  msgfile="$(mktemp)"
  git log -1 --format=%B "${P[$((m - k))]}" >"$msgfile"
  stamp="$((now - (m - k) * 60)) +0100"
  commit=$(GIT_AUTHOR_DATE="$stamp" GIT_COMMITTER_DATE="$stamp" \
    git commit-tree "$tree" ${parent:+-p "$parent"} -F "$msgfile")
  rm -f "$msgfile"
  parent="$commit"
done
git update-ref "refs/heads/$out" "$parent"
echo "$out -> $parent ($m commits)"
git diff --quiet "$base" "$out" && echo "tree identical to $base"
