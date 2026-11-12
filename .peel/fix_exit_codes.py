"""rebase --exec helper: the EXIT_FAILURE description names only the failures the tree can produce."""
import re
from pathlib import Path

console = Path("src/coinjoin_pipeline/console.py")
cli = Path("src/coinjoin_pipeline/cli.py")
if console.exists():
    parts = []
    if cli.exists() and "def pull(" in cli.read_text():
        parts.append("pull")
    if Path("src/coinjoin_pipeline/storage/s5client.py").exists():
        parts.append("S3 transfer")
    if Path("src/coinjoin_pipeline/watch.py").exists():
        parts.append("log source")
    if parts:
        listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " or " + parts[-1]
        text = f"a failed run reported as [ERROR], a runtime error, or a failed {listed}"
    else:
        text = "a failed run reported as [ERROR] or a runtime error"
    source = console.read_text()
    updated = re.sub(r'\(EXIT_FAILURE, "[^"]*"\),', f'(EXIT_FAILURE, "{text}"),', source)
    if updated != source:
        console.write_text(updated)
