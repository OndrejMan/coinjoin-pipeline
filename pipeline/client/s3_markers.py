"""S3 marker waiting and best-effort PBS cancellation helpers.

The functions here know the marker protocol, but not command-line parsing,
Kubernetes resource staging, or PBS graph submission.  Callers inject the
small runtime operations so ``client.wrapper`` remains the compatibility and
mocking facade during the refactor.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from client.artifacts import S3Access
from client.pbs import PBSError


def wait_for_s3_pbs_marker(
    *,
    stage: str,
    job_id: str,
    run_prefix: str,
    access: S3Access,
    walltime: str,
    wait_for_marker: Callable[..., None],
    pbs_probe: Callable[[str], Callable[[], str]],
    wait_timeout: Callable[[str], int],
) -> None:
    """Wait for one PBS stage using the common S3 marker/probe contract."""
    print(f"[full-run] Waiting for {stage} marker (PBS job {job_id})")
    wait_for_marker(
        stage,
        f"{run_prefix}/.pbs/{stage}.done",
        f"{run_prefix}/.pbs/{stage}.failed",
        access,
        timeout_seconds=wait_timeout(walltime),
        probe=pbs_probe(job_id),
    )


def cancel_dependent_pbs_job(
    stage_name: str,
    job_id: str,
    *,
    qdel_job: Callable[[str], bool],
) -> bool:
    """Cancel a dependent stage after an upstream wait failed, and say so."""
    print(
        f"[full-run] Cancelling dependent {stage_name} PBS job {job_id}",
        file=sys.stderr,
    )
    try:
        cancelled = qdel_job(job_id)
    except (OSError, PBSError, RuntimeError) as error:
        print(
            f"[full-run] Could not cancel {stage_name} job {job_id}: {error}",
            file=sys.stderr,
        )
        cancelled = False
    if not cancelled:
        print(
            f"[full-run] {stage_name} PBS job {job_id} may still be queued or "
            f"running; cancel it with: qdel {job_id}",
            file=sys.stderr,
        )
    return cancelled


def rollback_s3_pbs_submissions(
    submitted_jobs: list[tuple[str, str]],
    *,
    qdel_job: Callable[[str], bool],
) -> None:
    """Cancel every job obtained before an S3 PBS graph submission failed."""
    failed: list[tuple[str, str]] = []
    for stage, job_id in reversed(submitted_jobs):
        print(f"[pbs] Rolling back submitted {stage} job {job_id}", file=sys.stderr)
        try:
            cancelled = qdel_job(job_id)
        except (OSError, PBSError, RuntimeError) as error:
            print(
                f"[pbs] Could not roll back {stage} job {job_id}: {error}",
                file=sys.stderr,
            )
            cancelled = False
        if not cancelled:
            failed.append((stage, job_id))
    if failed:
        print(
            "[pbs] ROLLBACK INCOMPLETE: the following jobs may still be queued "
            "or running and will consume allocation until cancelled manually:",
            file=sys.stderr,
        )
        for stage, job_id in failed:
            print(f"[pbs]   {stage}: qdel {job_id}", file=sys.stderr)
    elif submitted_jobs:
        print(f"[pbs] Rolled back {len(submitted_jobs)} submitted job(s).", file=sys.stderr)
