"""rebase --exec helper: without shared-storage submission, qsub only takes script text."""
import subprocess
from pathlib import Path

submission = Path("src/coinjoin_pipeline/execution/pbs/submission.py")
if submission.exists() and not Path("src/coinjoin_pipeline/execution/pbs/submission_local.py").exists():
    text = submission.read_text()
    new = text
    for old, replacement in (
        ("def submit_pbs(script: Path | str", "def submit_pbs(script: str"),
        (
            '    """Submit a script file, or script text through stdin, and return the job ID.\n',
            '    """Submit script text through stdin and return the job ID.\n',
        ),
        ("    if isinstance(script, Path):\n        command.append(str(script))\n", ""),
        ("        input=None if isinstance(script, Path) else script,\n", "        input=script,\n"),
    ):
        new = new.replace(old, replacement)
    if new != text:
        submission.write_text(new)
        subprocess.run([".peel/fiximports.sh", str(submission)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
        print("submit:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
