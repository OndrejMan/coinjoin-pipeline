"""rebase --exec helper: worker_command names only the phases some job still runs."""
import re
import subprocess
from pathlib import Path

commands = Path("src/coinjoin_pipeline/execution/pbs/commands.py")
if commands.exists():
    text = commands.read_text()
    used = set(re.findall(r'worker_command\("(\w+)"', text))
    new = text
    if used and "analyze" not in used:
        new = new.replace('phase: Literal["run", "analyze", "report"],', 'phase: Literal["run", "report"],')
    if used == {"run"}:
        new = new.replace('phase: Literal["run", "report"],', 'phase: Literal["run"],')
        new = new.replace(
            '    if phase != "report":\n        arguments.extend(analysis_image_arguments(config))\n',
            "    arguments.extend(analysis_image_arguments(config))\n",
        )
        new = new.replace(
            '    python = "python3" if phase == "report" else BLOCKSCI_IMAGE_PYTHON_COMMAND\n'
            '    return python + " " + shlex.join(arguments)\n',
            '    return BLOCKSCI_IMAGE_PYTHON_COMMAND + " " + shlex.join(arguments)\n',
        )
    if new != text:
        commands.write_text(new)
        subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
        print("worker:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
