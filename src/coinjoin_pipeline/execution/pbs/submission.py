"""PBS submission, job IDs, cancellation and state probes shared by every PBS stage."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from coinjoin_pipeline.storage.s3 import (
    PROBE_QUEUED,
    PROBE_RUNNING,
    PROBE_TERMINAL,
    PROBE_UNKNOWN,
)

from .defaults import (
    PBS_ACTIVE_STATES,
    PBS_QUEUED_STATES,
    PBS_TERMINAL_STATES,
)
from .status import PBSQueryError, job_details
from .validation import (
    PBSError,
    require_qsub,
    require_safe_pbs_token,
)


def qstat_job_state(job_id: str) -> str | None:
    try:
        fields = job_details(job_id)
    except (PBSQueryError, FileNotFoundError):
        return None
    return "MISSING" if fields is None else fields.get("job_state")


def _parse_qsub_job_id(stdout: str) -> str:
    """Validate the job ID qsub printed on a zero exit.

    A zero exit with unusable stdout is worse than a failure: the scheduler may
    well have accepted the job, but an unrecorded job ID cannot be persisted,
    depended on, or cancelled during rollback. Fail loudly instead.
    """
    job_id = (stdout or "").strip()
    if not job_id or "\n" in job_id:
        raise PBSError(f"qsub returned an invalid job ID: {job_id!r}")
    # MetaCentrum job IDs are `<seq>.<server-fqdn>`, which SAFE_PBS_TOKEN_RE
    # covers. Job arrays (`123[].server`) would be rejected; no stage submits one.
    require_safe_pbs_token(job_id, "PBS job ID")
    return job_id


def qsub_command(
    dependency_job_id: str | Sequence[str] | None = None,
) -> list[str]:
    """Build qsub's common dependency prefix for file and stdin submissions."""
    command = ["qsub"]
    if not dependency_job_id:
        return command
    dependency_job_ids = (dependency_job_id,) if isinstance(dependency_job_id, str) else tuple(dependency_job_id)
    if any(not job_id for job_id in dependency_job_ids):
        raise PBSError("PBS dependency job IDs must not be empty")
    command.extend(["-W", f"depend=afterok:{':'.join(dependency_job_ids)}"])
    return command


def submit_pbs(
    script_path: Path,
    dependency_job_id: str | Sequence[str] | None = None,
) -> str:
    """Submit a PBS script via ``qsub`` and return the job ID."""
    command = qsub_command(dependency_job_id)
    command.append(str(script_path))
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise PBSError(f"qsub failed (exit {result.returncode}): {result.stderr.strip()}")
    return _parse_qsub_job_id(result.stdout)


def submit_pbs_text(
    script: str,
    dependency_job_id: str | Sequence[str] | None = None,
) -> str:
    """Submit a PBS script to ``qsub`` via stdin and return the job ID.

    Stdin submission avoids needing a script path visible to the PBS server,
    which the S3-compatible stages lack (no shared run directory).
    """
    command = qsub_command(dependency_job_id)
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


def persist_pbs_job_id(run_dir: Path, stage: str, job_id: str) -> None:
    """Record a submitted job ID atomically.

    A truncating write can be interrupted, and the overlap check skips empty
    .jobid files -- which would let a duplicate graph be submitted while the
    recorded job is still active. Write-then-rename never exposes a partial file.
    """
    marker_dir = run_dir / ".pbs"
    marker_dir.mkdir(parents=True, exist_ok=True)
    target = marker_dir / f"{stage}.jobid"
    temp_path = marker_dir / f".{stage}.jobid.tmp"
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(f"{job_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def qdel_pbs_job(job_id: str) -> bool:
    """Cancel a PBS job; return whether the cancellation was confirmed.

    Callers such as ``rollback_s3_pbs_submissions`` must be able to tell a
    cancelled job from one that is still burning allocation, so failures are
    reported rather than only printed.
    """
    if shutil.which("qdel") is None:
        print(f"[pbs] qdel unavailable; cannot cancel PBS job {job_id}", file=sys.stderr)
        return False
    result = subprocess.run(
        ["qdel", job_id],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # A job that already left the queue counts as cancelled for rollback.
        if "unknown job" in stderr.lower() or "job has finished" in stderr.lower():
            return True
        print(
            f"[pbs] qdel {job_id} failed (exit {result.returncode}): {stderr}",
            file=sys.stderr,
        )
        return False
    return True


def pbs_job_probe(job_id: str) -> Callable[[], str]:
    """Build a qstat-backed liveness probe for ``wait_for_s3_marker``."""

    def probe() -> str:
        state = qstat_job_state(job_id)
        if state in PBS_TERMINAL_STATES or state == "MISSING":
            return PROBE_TERMINAL
        if state is None:
            return PROBE_UNKNOWN
        if state in PBS_QUEUED_STATES:
            return PROBE_QUEUED
        if state in PBS_ACTIVE_STATES:
            return PROBE_RUNNING
        raise PBSError(f"PBS job has unexpected qstat state: {job_id} (state {state})")

    return probe


@dataclass(frozen=True)
class PBSJobSpec:
    stage: str
    script: str
    dependencies: tuple[str, ...] = ()


def submit_pbs_job(job: PBSJobSpec, dry_run: bool = False) -> str | None:
    if dry_run:
        print(f"[dry-run] PBS script for {job.stage}:\n{job.script}")
        return None
    require_qsub()
    job_id = submit_pbs_text(job.script, job.dependencies)
    print(f"[pbs] Submitted {job.stage} PBS job: {job_id}")
    return job_id
