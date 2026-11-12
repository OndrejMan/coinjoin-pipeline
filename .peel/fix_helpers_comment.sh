#!/bin/bash
# rebase --exec helper: keep the layout of templates_s3's helper constants after their removal.
cd "$(dirname "$0")/.." || exit 1
f=src/coinjoin_pipeline/execution/pbs/templates_s3.py
[ -f "$f" ] || exit 0
.venv/bin/python - "$f" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
t = p.read_text()
comment = "# Standalone helpers some jobs ship besides the worker.\n"
t = t.replace(")\n" + comment, ")\n\n" + comment)
t = t.replace(comment + "BLOCKSCI = ", "BLOCKSCI = ")
p.write_text(t)
PY
.peel/fiximports.sh "$f" >/dev/null
if ! git diff --quiet HEAD; then
  git commit -qa --amend --no-edit
  echo "fixed: $(git log -1 --format=%s)"
fi
