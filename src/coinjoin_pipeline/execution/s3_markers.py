"""S3 marker waiting and best-effort PBS cancellation for one S3 run."""

from __future__ import annotations

import sys

from coinjoin_pipeline.execution.pbs.submission import pbs_job_probe, qdel_pbs_job
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pbs_settings import pbs_wait_timeout
from coinjoin_pipeline.storage.s3 import S3Access, wait_for_s3_marker


def wait_for_s3_pbs_stage(*, stage: str, job_id: str, run_prefix: str, access: S3Access, walltime: str) -> None:
    """Wait for one PBS stage using the common S3 marker/probe contract."""
    print(f"[full-run] Waiting for {stage} marker (PBS job {job_id})")
    wait_for_s3_marker(
        stage,
        f"{run_prefix}/.pbs/{stage}.done",
        f"{run_prefix}/.pbs/{stage}.failed",
        access,
        timeout_seconds=pbs_wait_timeout(walltime),
        probe=pbs_job_probe(job_id),
    )


def cancel_dependent_pbs_job(stage_name: str, job_id: str) -> bool:
    """Cancel a dependent stage after an upstream wait failed, and say so.

    Reports the recovery command when qdel is missing or refused, so the
    operator is never told a job was cancelled when it is still queued.
    """
    print(
        f"[full-run] Cancelling dependent {stage_name} PBS job {job_id}",
        file=sys.stderr,
    )
    try:
        cancelled = qdel_pbs_job(job_id)
    except (OSError, PBSError, RuntimeError) as error:
        print(
            f"[full-run] Could not cancel {stage_name} job {job_id}: {error}",
            file=sys.stderr,
        )
        cancelled = False
    if not cancelled:
        print(
            f"[full-run] {stage_name} PBS job {job_id} may still be queued or running; cancel it with: qdel {job_id}",
            file=sys.stderr,
        )
    return cancelled


def rollback_s3_pbs_submissions(submitted_jobs: list[tuple[str, str]]) -> None:
    """Cancel every job obtained before an S3 PBS graph submission failed.

    Rollback is best effort: report exactly which jobs are still queued or
    running so the operator can finish the job, instead of implying the graph
    was fully withdrawn.
    """
    failed: list[tuple[str, str]] = []
    for stage, job_id in reversed(submitted_jobs):
        print(f"[pbs] Rolling back submitted {stage} job {job_id}", file=sys.stderr)
        try:
            cancelled = qdel_pbs_job(job_id)
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
        print(
            f"[pbs] Rolled back {len(submitted_jobs)} submitted job(s).",
            file=sys.stderr,
        )
