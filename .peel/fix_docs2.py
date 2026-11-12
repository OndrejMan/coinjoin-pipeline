"""rebase --exec helper: docstrings that name helpers or readers a peel removed."""
import subprocess
from pathlib import Path

changed = False


def text_of(path: str) -> str:
    p = Path(path)
    return p.read_text() if p.exists() else ""


def edit(path: str, old: str, new: str) -> None:
    global changed
    p = Path(path)
    if not p.exists():
        return
    text = p.read_text()
    if old in text:
        p.write_text(text.replace(old, new))
        changed = True


kubernetes = text_of("src/coinjoin_pipeline/execution/kubernetes.py")
markers = text_of("src/coinjoin_pipeline/execution/s3_markers.py")
has_job_probe = "def kubernetes_job_probe" in kubernetes
has_marker_wait = "def wait_for_s3_marker" in markers

PROBES_FULL = '''"""What a probe of a remote job reports while the frontend waits for that job's S3 marker.

A PBS job (``pbs.submission.pbs_job_probe``) and the Kubernetes emulation Job
(``kubernetes.kubernetes_job_probe``) both answer with one of these states;
``s3_markers.wait_for_s3_marker`` decides its deadlines by them.
"""'''
PROBES_PBS = '''"""What a probe of a remote job reports while the frontend waits for that job's S3 marker.

A PBS job (``pbs.submission.pbs_job_probe``) answers with one of these states;
``s3_markers.wait_for_s3_marker`` decides its deadlines by them.
"""'''
PROBES_NONE = '''"""What a probe of a PBS job (``pbs.submission.pbs_job_probe``) reports about its state."""'''
if not has_job_probe:
    edit("src/coinjoin_pipeline/execution/probes.py", PROBES_FULL, PROBES_PBS)
if not has_marker_wait:
    edit("src/coinjoin_pipeline/execution/probes.py", PROBES_PBS, PROBES_NONE)
    edit(
        "src/coinjoin_pipeline/execution/pbs/submission.py",
        '    """Build a qstat-backed liveness probe for ``wait_for_s3_marker``."""',
        '    """Build a qstat-backed liveness probe of one PBS job."""',
    )
if not Path("src/coinjoin_pipeline/watch.py").exists() and not Path("src/coinjoin_pipeline/stop.py").exists():
    edit(
        "src/coinjoin_pipeline/execution/pbs/submission.py",
        '    """``<run>/.pbs/<stage>.jobid``: read by watch, overlap detection, cancellation and scenario-lab."""',
        '    """``<run>/.pbs/<stage>.jobid``: read by overlap detection and scenario-lab."""',
    )
submission_text = text_of("src/coinjoin_pipeline/execution/pbs/submission.py")
FULL = """    well have accepted the job, but an unrecorded job ID cannot be persisted,
    depended on, or cancelled during rollback. Fail loudly instead."""
NO_DEPEND = """    well have accepted the job, but an unrecorded job ID cannot be persisted or
    cancelled during rollback. Fail loudly instead."""
ROLLBACK_ONLY = """    well have accepted the job, but an unrecorded job ID cannot be cancelled
    during rollback. Fail loudly instead."""
if submission_text and "depend=afterok" not in submission_text:
    edit("src/coinjoin_pipeline/execution/pbs/submission.py", FULL, NO_DEPEND)
    if "def persist_pbs_job_id" not in submission_text:
        edit("src/coinjoin_pipeline/execution/pbs/submission.py", NO_DEPEND, ROLLBACK_ONLY)

kube_doc_full = '"""The S3 emulation Job on Kubernetes: kubectl access, preflight, resources, probe, diagnostics and cleanup."""'
if "def collect_s3_emulation_diagnostics" not in kubernetes and "def kubernetes_job_probe" in kubernetes:
    edit(
        "src/coinjoin_pipeline/execution/kubernetes.py",
        kube_doc_full,
        '"""The S3 emulation Job on Kubernetes: kubectl access, preflight, resources, probe and cleanup."""',
    )
if "def kubernetes_job_probe" not in kubernetes:
    for current in (kube_doc_full, '"""The S3 emulation Job on Kubernetes: kubectl access, preflight, resources, probe and cleanup."""'):
        edit(
            "src/coinjoin_pipeline/execution/kubernetes.py",
            current,
            '"""The S3 emulation Job on Kubernetes: kubectl access, preflight, resources and cleanup."""',
        )
if changed:
    subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
    print("docs:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
