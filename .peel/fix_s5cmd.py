"""rebase --exec helper: without the exporter sync, no s5cmd version gate is needed."""
import subprocess
from pathlib import Path

staging = Path("src/coinjoin_pipeline/execution/s3_staging.py")
s3 = Path("src/coinjoin_pipeline/storage/s3.py")
if s3.exists() and (not staging.exists() or "def upload_exporters" not in staging.read_text()):
    text = s3.read_text()
    if "def require_s5cmd_version" in text:
        text = text.replace("    require_s5cmd_version()\n", "")
        s3.write_text(text)
        subprocess.run(
            [".venv/bin/python", ".peel/peel.py", "def", str(s3), "require_s5cmd_version", "s5cmd_version",
             "MINIMUM_S5CMD_VERSION", "S5CMD_VERSION_RE"],
            check=True,
        )
        subprocess.run([".peel/fiximports.sh", str(s3)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
        print("s5cmd:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
