#!/usr/bin/env bash
# Cheap per-step gate: everything left compiles, ruff finds no undefined names or unused
# imports, and every remaining package module imports.
set -uo pipefail
cd "$(dirname "$0")/.."
status=0
mapfile -t py < <(git ls-files -c -o --exclude-standard '*.py' | while read -r f; do [ -f "$f" ] && echo "$f"; done)
if [ ${#py[@]} -gt 0 ]; then
  .venv/bin/python -m py_compile "${py[@]}" 2>&1 | head -20 || status=1
  .venv/bin/ruff check --no-cache --select F,E9 --quiet "${py[@]}" || status=1
fi
if [ -d src/coinjoin_pipeline ]; then
  PYTHONPATH=src:pipeline .venv/bin/python - <<'PY' || status=1
import importlib, pkgutil, sys
failed = []
roots = [("coinjoin_pipeline", "src/coinjoin_pipeline")]
import os
if os.path.isdir("pipeline/exporters"):
    roots.append(("exporters", "pipeline/exporters"))
for package, path in roots:
    try:
        importlib.import_module(package)
    except Exception as error:
        failed.append(f"{package}: {error!r}")
        continue
    for info in pkgutil.walk_packages([path], package + "."):
        name = info.name
        if name.endswith("blocksci_export.detector") or name.endswith("templates.blocksci_notebook"):
            pass
        try:
            importlib.import_module(name)
        except SystemExit:
            pass
        except Exception as error:
            failed.append(f"{name}: {error!r}")
if failed:
    print("\n".join(failed))
    sys.exit(1)
PY
fi
if [ -d tests ] && ls tests/unit/test_*.py tests/pipeline/test_*.py >/dev/null 2>&1; then
  PYTHONPATH=src:pipeline .venv/bin/python -m pytest -q --collect-only -p no:cacheprovider >"${TMPDIR:-/tmp/claude-1000}/collect.log" 2>&1 || { tail -15 "${TMPDIR:-/tmp/claude-1000}/collect.log"; status=1; }
fi
for f in $(git ls-files -c -o --exclude-standard '*.sh' | grep -v '/templates/'); do [ -f "$f" ] && { bash -n "$f" || status=1; }; done
if [ -d src/coinjoin_pipeline ] && grep -q "^\[tool.mypy\]" pyproject.toml 2>/dev/null; then
  typing=$(.venv/bin/mypy --no-error-summary 2>&1) || { echo "$typing" | head -15; status=1; }
fi
unformatted=$(.peel/fmt_check.sh)
[ -n "$unformatted" ] && { echo "$unformatted"; status=1; }
layout=$(.venv/bin/python .peel/layout_check.py target)
[ "$layout" != "no new anomalies" ] && { echo "$layout"; status=1; }
stale=$(.peel/stale_paths.sh)
[ -n "$stale" ] && { echo "$stale"; status=1; }
exit $status
