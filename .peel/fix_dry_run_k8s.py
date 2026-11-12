"""rebase --exec helper: the dry-run notice mentions Kubernetes only while the tree can create Kubernetes resources."""
import subprocess
from pathlib import Path

orchestrator = Path("src/coinjoin_pipeline/execution/orchestrator.py")
old = 'print("[dry-run] No containers, files, reports, or Kubernetes resources will be created.")'
new = 'print("[dry-run] No containers, files or reports will be created.")'
k8s = subprocess.run(["git", "grep", "-q", "-E", "kubectl", "--", "src"], capture_output=True).returncode == 0
if orchestrator.exists() and not k8s and old in orchestrator.read_text():
    orchestrator.write_text(orchestrator.read_text().replace(old, new))
