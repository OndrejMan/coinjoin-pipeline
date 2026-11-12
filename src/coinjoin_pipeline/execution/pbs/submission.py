"""PBS submission, job IDs, cancellation and state probes shared by every PBS stage."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from exporters.artifact_paths import PBS_MARKER_DIR, pbs_marker

from coinjoin_pipeline.execution.probes import PROBE_UNKNOWN

from .status import (
    LEFT_SCHEDULER,
    PBS_COMMAND_TIMEOUT_SECONDS,
    PBSQueryError,
    job_details,
    job_left_scheduler,
    job_phase,
)
from .validation import (
    PBSError,
    require_qsub,
    require_safe_pbs_token,
)


def qstat_job_state(job_id: str) -> str | None:
    """The job's qstat state, ``LEFT_SCHEDULER`` once PBS no longer knows it, None when qstat cannot tell."""
    try:
        fields = job_details(job_id)
    except (PBSQueryError, FileNotFoundError):
        return None
    return LEFT_SCHEDULER if fields is None else fields.get("job_state")


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


def qsub_command(dependencies: Sequence[str] = ()) -> list[str]:
    """Build qsub's common dependency prefix for file and stdin submissions."""
    if any(not job_id for job_id in dependencies):
        raise PBSError("PBS dependency job IDs must not be empty")
    return ["qsub", "-W", f"depend=afterok:{':'.join(dependencies)}"] if dependencies else ["qsub"]


def submit_pbs(script: Path | str, dependencies: Sequence[str] = ()) -> str:
    """Submit a script file, or script text through stdin, and return the job ID.

    Stdin submission avoids needing a script path visible to the PBS server,
    which the S3-compatible stages lack (no shared run directory).
    """
    command = qsub_command(dependencies)
    if isinstance(script, Path):
        command.append(str(script))
    result = subprocess.run(
        command,
        check=False,
        text=True,
        input=None if isinstance(script, Path) else script,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise PBSError(f"qsub failed (exit {result.returncode}): {result.stderr.strip()}")
    return _parse_qsub_job_id(result.stdout)


def pbs_job_id_path(run_dir: Path, stage: str) -> Path:
    """``<run>/.pbs/<stage>.jobid``: read by watch, overlap detection, cancellation and scenario-lab."""
    return run_dir / pbs_marker(stage, "jobid")


def read_pbs_job_id(run_dir: Path, stage: str) -> str | None:
    """The recorded job ID of one stage, or None when none was persisted."""
    path = pbs_job_id_path(run_dir, stage)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def recorded_pbs_job_ids(run_dir: Path) -> dict[str, str]:
    """Every recorded job ID of the run by stage; the files are never pruned, so finished jobs stay."""
    jobs: dict[str, str] = {}
    for path in sorted((run_dir / PBS_MARKER_DIR).glob("*.jobid")):
        job_id = read_pbs_job_id(run_dir, path.stem)
        if job_id:
            jobs[path.stem] = job_id
    return jobs


def persist_pbs_job_id(run_dir: Path, stage: str, job_id: str) -> None:
    """Record a submitted job ID atomically.

    A truncating write can be interrupted, and the overlap check skips empty
    .jobid files -- which would let a duplicate graph be submitted while the
    recorded job is still active. Write-then-rename never exposes a partial file.
    """
    target = pbs_job_id_path(run_dir, stage)
    marker_dir = target.parent
    marker_dir.mkdir(parents=True, exist_ok=True)
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


def pbs_job_probe(job_id: str) -> Callable[[], str]:
    """Build a qstat-backed liveness probe for ``wait_for_s3_marker``."""

    def probe() -> str:
        state = qstat_job_state(job_id)
        if state is None:
            return PROBE_UNKNOWN
        phase = job_phase(state)
        if phase is None:
            raise PBSError(f"PBS job has unexpected qstat state: {job_id} (state {state})")
        return phase

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
    job_id = submit_pbs(job.script, job.dependencies)
    print(f"[pbs] Submitted {job.stage} PBS job: {job_id}")
    return job_id
