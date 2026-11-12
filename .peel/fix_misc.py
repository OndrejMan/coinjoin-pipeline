"""rebase --exec helper: small text residues of peeled features."""
import subprocess
from pathlib import Path

changed = False


def edit(path: str, old: str, new: str, when=lambda: True) -> None:
    global changed
    p = Path(path)
    if not p.exists():
        return
    text = p.read_text()
    if old in text and when():
        p.write_text(text.replace(old, new))
        changed = True


def blocksci_job_publishes() -> bool:
    jobs = Path("src/coinjoin_pipeline/execution/pbs/blocksci_jobs.py")
    if not jobs.exists():
        return False
    text = jobs.read_text()
    start = text.find("def blocksci(stage: Stage)")
    end = text.find("\ndef ", start + 1)
    return start >= 0 and "publish_index(" in text[start:end]


def has_index_lock() -> bool:
    index = Path("src/coinjoin_pipeline/execution/pbs/blocksci_index.py")
    return index.exists() and "def open_index" in index.read_text()


edit(
    "src/coinjoin_pipeline/execution/pbs/templates_s3.py",
    '"""Parse and analyze in one job, publishing the same index archive as the split workflow."""',
    '"""Parse and analyze in one job."""',
    lambda: not blocksci_job_publishes(),
)
edit(
    "src/coinjoin_pipeline/execution/pbs/stage.py",
    "    # Before every later finalizer, the index lock release included.\n",
    "    # Before every later finalizer.\n",
    lambda: not has_index_lock(),
)
edit(
    "src/coinjoin_pipeline/execution/pbs/blocksci_jobs.py",
    "    CommandSettings,\n    BlocksciSettings,\n",
    "    BlocksciSettings,\n    CommandSettings,\n",
)
def no_task_option() -> bool:
    configuration = Path("src/coinjoin_pipeline/configuration.py")
    return configuration.exists() and 'name="blocksci_task"' not in configuration.read_text()


edit(
    "src/coinjoin_pipeline/arguments.py",
    '"blocksci": ("BlockSci", "Reusable tasks are submitted with pbs-from-s3 --blocksci-pbs."),',
    '"blocksci": ("BlockSci", None),',
    no_task_option,
)
def no_workflow_option() -> bool:
    configuration = Path("src/coinjoin_pipeline/configuration.py")
    return configuration.exists() and 'name="blocksci_workflow"' not in configuration.read_text()


edit(
    "src/coinjoin_pipeline/execution/stages.py",
    """    report is a separate stage for every detect workflow, including resumes
    that consume a previously stored baseline, unless ``unified_report`` is
    false: a full-run without coinjoin-analysis has no baseline to compare.
""",
    """    report is a separate stage, including for resumes that consume a
    previously stored baseline, unless ``unified_report`` is false: a full-run
    without coinjoin-analysis has no baseline to compare.
""",
    no_workflow_option,
)
if changed:
    subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
    print("misc:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
