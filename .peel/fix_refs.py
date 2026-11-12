"""rebase --exec helper: no file names a doc or an example the tree does not have yet."""
import subprocess
from pathlib import Path

changed = False


def edit(path: str, old: str, new: str, missing: str) -> None:
    global changed
    p = Path(path)
    if p.exists() and not Path(missing).exists():
        text = p.read_text()
        if old in text:
            p.write_text(text.replace(old, new))
            changed = True


edit(
    "src/coinjoin_pipeline/arguments.py",
    '"Run one upstream program instead of the default baseline; see docs/coinjoin-analysis-operations.md.",',
    '"Run one upstream program instead of the default baseline.",',
    "docs/coinjoin-analysis-operations.md",
)
edit(
    "src/coinjoin_pipeline/arguments.py",
    '"Only for action mainnet-sync; see docs/mainnet-synchronization.md."',
    '"Only for action mainnet-sync."',
    "docs/mainnet-synchronization.md",
)
edit(
    "scripts/render-mainnet-restore-check.py",
    'path = Path(sys.argv[1] if len(sys.argv) > 1 else "examples/mainnet-sync.yaml")',
    "path = Path(sys.argv[1])",
    "examples/mainnet-sync.yaml",
)
if changed:
    subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
    print("refs:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
