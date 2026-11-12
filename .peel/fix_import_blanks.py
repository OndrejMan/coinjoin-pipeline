"""rebase --exec helper: collapse the double blank line an import removal left between import groups."""
import re
import subprocess
from pathlib import Path

PATTERN = re.compile(r"^((?:from|import) [^\n]*)\n\n\n(?=(?:from|import) )", re.M)
files = subprocess.run(["git", "ls-files", "*.py"], capture_output=True, text=True, check=True).stdout.split()
for name in files:
    path = Path(name)
    if not path.is_file():
        continue
    text = path.read_text()
    if not PATTERN.search(text):
        continue
    target = subprocess.run(["git", "show", f"target:{name}"], capture_output=True, text=True)
    if target.returncode == 0 and PATTERN.search(target.stdout):
        continue
    path.write_text(PATTERN.sub(r"\1\n\n", text))
