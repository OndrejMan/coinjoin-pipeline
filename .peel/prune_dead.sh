#!/bin/bash
# rebase --exec helper: remove symbols a peel left without users, then amend.
cd "$(dirname "$0")/.." || exit 1
py=.venv/bin/python
only_def() {  # file symbol: true when the symbol occurs only in its own file, once
  local hits
  hits=$(git grep -c -w "$2" -- src pipeline tests scripts container 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
  [ -f "$1" ] && grep -q -w "^$2\|^def $2" "$1" && [ "$hits" -eq 1 ]
}
for pair in "pipeline/exporters/artifact_paths.py REPORT_MARKDOWN_FILE" \
            "pipeline/exporters/artifact_paths.py REPORT_MARKDOWN" \
            "pipeline/exporters/artifact_paths.py FALSE_CJTXS_FILE" \
            "src/coinjoin_pipeline/storage/s3.py shell_value" \
            "src/coinjoin_pipeline/execution/pbs/templates_s3.py EXECUTION_DIR" \
            "src/coinjoin_pipeline/storage/s3.py delete_s3_object_if_present" \
            "src/coinjoin_pipeline/option_rules.py _resources_supplied"; do
  set -- $pair
  if only_def "$1" "$2"; then $py .peel/peel.py def "$1" "$2" && .peel/fiximports.sh "$1" >/dev/null; fi
done
commands=src/coinjoin_pipeline/execution/pbs/commands.py
if [ -f "$commands" ] && ! [ -f src/coinjoin_pipeline/execution/shared_storage_pbs.py ] \
   && ! [ -f tests/pipeline/test_pbs.py ] && grep -q "    blocksci_script: str | None = None," "$commands"; then
  $py - "$commands" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
t = p.read_text()
old = '''def blocksci_pbs_command(
    run_id: str,
    config: PipelineConfiguration,
    include_report: bool = True,
    blocksci_script: str | None = None,
) -> str:
    command = worker_command("run", run_id, config)
    if blocksci_script:
        command += " --script " + shlex.quote(blocksci_script)
'''
new = '''def blocksci_pbs_command(run_id: str, config: PipelineConfiguration, include_report: bool = True) -> str:
    command = worker_command("run", run_id, config)
'''
assert t.count(old) == 1, "blocksci_pbs_command shape changed"
p.write_text(t.replace(old, new))
PY
fi
scenarios=src/coinjoin_pipeline/execution/scenarios.py
if [ -f "$scenarios" ] && grep -q "^def validate_scenario" "$scenarios" \
   && [ "$(git grep -l -w validate_scenario -- src pipeline scripts | grep -v "^$scenarios$" | wc -l)" -eq 0 ]; then
  $py .peel/peel.py def "$scenarios" validate_scenario fund_value load_scenario
  sed -i 's|^"""Scenario discovery, validation, and host/container scenario paths."""$|"""Scenario discovery and host/container scenario paths."""|' "$scenarios"
  .peel/fiximports.sh "$scenarios" >/dev/null
  git rm -q tests/pipeline/test_scenarios.py
fi
if ! git diff --quiet HEAD; then
  git add -A src pipeline tests
  git commit -q --amend --no-edit
  echo "pruned: $(git log -1 --format=%s)"
fi
