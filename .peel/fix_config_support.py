"""rebase --exec helper: the test configuration helper ships only with a test that imports it."""
import subprocess
from pathlib import Path

helper = Path("tests/pipeline/config_support.py")
used = subprocess.run(
    ["git", "grep", "-q", "config_support", "--", "tests", ":!tests/pipeline/config_support.py"], capture_output=True
).returncode == 0
if helper.exists() and not used:
    subprocess.run(["git", "rm", "-q", str(helper)], check=True)
