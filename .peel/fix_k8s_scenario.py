"""rebase --exec helper: the small Kubernetes scenario ships only with the example that runs it."""
import subprocess
from pathlib import Path

scenario = Path("scenarios/overactive-k8s-small.json")
used = subprocess.run(["git", "grep", "-q", "overactive-k8s-small", "--", ":!scenarios"], capture_output=True)
if scenario.exists() and used.returncode != 0:
    subprocess.run(["git", "rm", "-q", str(scenario)], check=True)
