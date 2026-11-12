"""rebase --exec helper: PBS/Kubernetes environment handling only in trees that have PBS or Kubernetes."""
import subprocess
from pathlib import Path


def mentions(pattern: str, *paths: str) -> bool:
    return subprocess.run(["git", "grep", "-q", "-E", pattern, "--", *paths], capture_output=True).returncode == 0


manifest = Path("src/coinjoin_pipeline/manifest.py")
if manifest.exists():
    text = manifest.read_text()
    if not mentions("kubectl|KUBERNETES_", "src", ":!src/coinjoin_pipeline/manifest.py"):
        text = text.replace('    "KUBERNETES_",\n', "")
    if not Path("src/coinjoin_pipeline/execution/pbs").is_dir():
        text = text.replace('    "PBS_",\n', "")
    manifest.write_text(text)

worker = Path("pipeline/exporters/worker.py")
if worker.exists() and not mentions("PBS_RUN_ID", "src"):
    text = worker.read_text()
    text = text.replace(
        'run_id = os.environ.get("PBS_RUN_ID") or os.environ.get("ACTIVE_RUN_ID")',
        'run_id = os.environ.get("ACTIVE_RUN_ID")',
    ).replace("--run-dir or ACTIVE_RUN_ID/PBS_RUN_ID is required", "--run-dir or ACTIVE_RUN_ID is required")
    worker.write_text(text)
