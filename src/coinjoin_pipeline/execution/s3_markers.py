"""Best-effort PBS cancellation for one S3 run."""

from __future__ import annotations

import sys

from coinjoin_pipeline.execution.pbs.submission import qdel_pbs_job
from coinjoin_pipeline.execution.pbs.validation import PBSError


def _cancel(log_prefix: str, stage: str, job_id: str) -> bool:
    """qdel one job; a scheduler or spawn error counts as not cancelled, reported with the stage."""
    try:
        return qdel_pbs_job(job_id)
    except (OSError, PBSError, RuntimeError) as error:
        print(f"[{log_prefix}] Could not cancel {stage} job {job_id}: {error}", file=sys.stderr)
        return False


def rollback_s3_pbs_submissions(submitted_jobs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Cancel every job obtained before an S3 PBS graph submission failed.

    Rollback is best effort: report exactly which jobs are still queued or
    running so the operator can finish the job, instead of implying the graph
    was fully withdrawn. Returns those ``(stage, job_id)`` pairs.
    """
    failed: list[tuple[str, str]] = []
    for stage, job_id in reversed(submitted_jobs):
        print(f"[pbs] Rolling back submitted {stage} job {job_id}", file=sys.stderr)
        if not _cancel("pbs", stage, job_id):
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
    return failed
