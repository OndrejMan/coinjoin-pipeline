"""PBS submission shared by every PBS stage."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .validation import (
    PBSError,
    require_qsub,
    require_safe_pbs_token,
)


def _parse_qsub_job_id(stdout: str) -> str:
    """Validate the job ID qsub printed on a zero exit.

    A zero exit with unusable stdout is worse than a failure: the scheduler may
    well have accepted a job nobody could then follow. Fail loudly instead.
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
