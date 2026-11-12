"""rebase --exec helper: drop mentions of the peeled external comparison from PBS docstrings."""
import subprocess
from pathlib import Path

REPLACEMENTS = [
    ("detection, an external comparison, a user script or a notebook", "detection, a user script or a notebook"),
    ("Detection, an external comparison, a user script or a notebook", "Detection, a user script or a notebook"),
    ("detection, an external comparison or a user script", "detection or a user script"),
    ("Detection, an external comparison or a user script", "Detection or a user script"),
    ("the manifest copy the external comparison reads", "the manifest copy the report reads"),
    ("``dumplings-snapshot.json`` for the external comparison.", "``dumplings-snapshot.json`` for the report."),
]
FILES = [
    "src/coinjoin_pipeline/execution/pbs/analysis_modes.py",
    "src/coinjoin_pipeline/execution/pbs/blocksci_analysis_jobs.py",
    "src/coinjoin_pipeline/execution/pbs/stage_settings.py",
    "src/coinjoin_pipeline/execution/pbs/templates_s3.py",
]
changed = False
for name in FILES:
    path = Path(name)
    if not path.exists():
        continue
    text = path.read_text()
    new = text
    for old, replacement in REPLACEMENTS:
        new = new.replace(old, replacement)
    if new != text:
        path.write_text(new)
        changed = True
if changed:
    subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
    print("docfix:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
