"""PBS submission and cancellation shared by every PBS stage."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from .status import (
    PBS_COMMAND_TIMEOUT_SECONDS,
    job_left_scheduler,
)
from .validation import (
    PBSError,
    require_qsub,
    require_safe_pbs_token,
)


def _parse_qsub_job_id(stdout: str) -> str:
    """Validate the job ID qsub printed on a zero exit.

    A zero exit with unusable stdout is worse than a failure: the scheduler may
    well have accepted the job, but an unrecorded job ID cannot be cancelled
    during rollback. Fail loudly instead.
    """
    job_id = (stdout or "").strip()
    if not job_id or "\n" in job_id:
        raise PBSError(f"qsub returned an invalid job ID: {job_id!r}")
    # MetaCentrum job IDs are `<seq>.<server-fqdn>`, which SAFE_PBS_TOKEN_RE
    # covers. Job arrays (`123[].server`) would be rejected; no stage submits one.
    require_safe_pbs_token(job_id, "PBS job ID")
    return job_id


def submit_pbs(script: str) -> str:
    """Submit script text through stdin and return the job ID.

    Stdin submission avoids needing a script path visible to the PBS server,
    which the S3-compatible stages lack (no shared run directory).
    """
    command = ["qsub"]
    result = subprocess.run(
        command,
        check=False,
        text=True,
        input=script,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise PBSError(f"qsub failed (exit {result.returncode}): {result.stderr.strip()}")
    return _parse_qsub_job_id(result.stdout)


def qdel_error(job_id: str) -> str | None:
    """Cancel a PBS job; None when the cancellation is confirmed, else why it is not.

    A job that already left the queue counts as cancelled.
    """
    if shutil.which("qdel") is None:
        return f"qdel unavailable; cannot cancel PBS job {job_id}"
    try:
        result = subprocess.run(
            ["qdel", job_id],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=PBS_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"qdel {job_id} timed out after {PBS_COMMAND_TIMEOUT_SECONDS} s"
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if job_left_scheduler(stderr):
            return None
        return f"qdel {job_id} failed (exit {result.returncode}): {stderr}"
    return None


def qdel_pbs_job(job_id: str) -> bool:
    """Cancel a PBS job; return whether the cancellation was confirmed.

    Callers such as ``rollback_s3_pbs_submissions`` must be able to tell a
    cancelled job from one that is still burning allocation, so failures are
    reported rather than only printed.
    """
    error = qdel_error(job_id)
    if error is not None:
        print(f"[pbs] {error}", file=sys.stderr)
        return False
    return True


@dataclass(frozen=True)
class PBSJobSpec:
    stage: str
    script: str


def submit_pbs_job(job: PBSJobSpec, dry_run: bool = False) -> str | None:
    if dry_run:
        print(f"[dry-run] PBS script for {job.stage}:\n{job.script}")
        return None
    require_qsub()
    job_id = submit_pbs(job.script)
    print(f"[pbs] Submitted {job.stage} PBS job: {job_id}")
    return job_id
