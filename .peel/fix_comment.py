"""rebase --exec helper: drop the comment of the Job overrides once they are gone."""
import subprocess
from pathlib import Path

p = Path("src/coinjoin_pipeline/configuration.py")
if p.exists():
    text = p.read_text()
    comment = "    # Overrides the S3 emulation Job forwards to its controller; the emulator's defaults apply when unset.\n"
    if comment in text and 'name="distributor_startup_timeout"' not in text:
        p.write_text(text.replace(comment, ""))
        subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
        print("comment:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
