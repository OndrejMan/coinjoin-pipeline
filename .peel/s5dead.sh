#!/bin/bash
# rebase --exec helper: drop candidate S5Client methods nothing calls any more, then amend.
cd "$(dirname "$0")/.." || exit 1
file=src/coinjoin_pipeline/storage/s5client.py
[ -f "$file" ] || exit 0
changed=1
while [ $changed -eq 1 ]; do
  changed=0
  for name in sizes object_size require_size copy_text cat cat_json; do
    grep -q "    def $name(" "$file" || continue
    if ! git grep -q "\.$name(" -- src tests pipeline scripts 2>/dev/null; then
      .venv/bin/python .peel/peel.py member "$file" S5Client "$name" && changed=1
    fi
  done
done
.peel/fiximports.sh "$file" >/dev/null
if ! git diff --quiet; then
  git commit -qa --amend --no-edit
  echo "pruned: $(git log -1 --format=%s)"
fi
