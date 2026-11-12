"""rebase --exec helper: the lint entry point mentions CI only in trees that have a CI workflow."""
from pathlib import Path

lint = Path("src/coinjoin_pipeline/lint.py")
old = "    # CI passes --check so a run reports problems instead of rewriting files.\n"
new = "    # --check reports problems instead of rewriting files.\n"
if lint.exists() and not Path(".github/workflows").is_dir() and old in lint.read_text():
    lint.write_text(lint.read_text().replace(old, new))
