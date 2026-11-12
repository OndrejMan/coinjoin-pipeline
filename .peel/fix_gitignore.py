"""rebase --exec helper: ignore generated paths only while something in the tree produces them."""
import re
import subprocess
from pathlib import Path


def mentions(pattern: str, *paths: str) -> bool:
    return subprocess.run(["git", "grep", "-q", pattern, "--", *paths], capture_output=True).returncode == 0


ignore = Path(".gitignore")
text = ignore.read_text()
if not mentions("github-test-results", ":!.gitignore"):
    text = text.replace("github-test-results/\n", "")
if not mentions("btc-data", "src"):
    text = text.replace("src/coinjoin_pipeline/btc-data/\n", "")
ignore.write_text(re.sub(r"\n{3,}", "\n\n", text))
