"""rebase --exec helper: the host manifest records a run mode only while external runs exist."""
import subprocess
from pathlib import Path

runs = Path("src/coinjoin_pipeline/runs.py")
line = '    existing.setdefault("mode", "emulator")\n'
external = subprocess.run(["git", "grep", "-q", '"external"', "--", "src"], capture_output=True).returncode == 0
if runs.exists() and not external and line in runs.read_text():
    runs.write_text(runs.read_text().replace(line, ""))
