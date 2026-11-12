"""rebase --exec helper: the stage-graph docstrings name only consumers that still exist."""
import subprocess
from pathlib import Path

stages = Path("src/coinjoin_pipeline/execution/stages.py")
if stages.exists():
    text = stages.read_text()
    has_full_run = Path("src/coinjoin_pipeline/execution/s3_workflow.py").exists()
    has_dependencies = "depend=afterok" in Path("src/coinjoin_pipeline/execution/pbs/submission.py").read_text() if Path(
        "src/coinjoin_pipeline/execution/pbs/submission.py"
    ).exists() else False
    has_plan = "def s3_full_run_plan" in text
    target = """graph, defined once. Submission derives its scheduler dependencies from it, the
orchestrator its wait order and cancellation policy, and ``--dry-run`` prints it.
"""
    v1 = """graph, defined once. Submission derives its scheduler dependencies from it and
``--dry-run`` prints it.
"""
    v2 = """graph, defined once. Submission derives an invocation's PBS jobs from it and
``--dry-run`` prints them.
"""
    v3 = """graph, defined once, which the local executor runs in order.
"""
    wanted = target if has_full_run else v1 if has_dependencies else v2 if has_plan else v3
    new = text
    for variant in (target, v1, v2, v3):
        new = new.replace(variant, wanted)
    if not has_dependencies:
        new = new.replace(
            "    runner translates them into local ordering or scheduler dependencies.\n",
            "    runner translates them into local ordering.\n",
        )
    if not has_plan:
        new = new.replace(
            '''    """Declare the experimental local graph: the same analyzer/report dependencies
    as ``s3_full_run_plan``, without an emulation node. Its node order is the
    serial execution order."""''',
            '''    """Declare the experimental local graph: both analyzers, then the report that
    joins them. Its node order is the serial execution order."""''',
        )
    if new != text:
        stages.write_text(new)
        subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
        print("stages:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
