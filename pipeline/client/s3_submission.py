"""State shared while one S3 PBS graph is being submitted."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from client.artifacts import S3Access


@dataclass(frozen=True)
class S3SubmissionOperations:
    """Marker and job-recording operations resolved by the wrapper facade."""

    clear_stage_markers: Callable[[S3Access, str, str, str], None]
    persist_job_id: Callable[[Path, str, str], None]


@dataclass
class S3SubmissionTracker:
    """Prepare marker state and remember every job for rollback on failure."""

    args: argparse.Namespace
    access: S3Access
    submitted_jobs: list[tuple[str, str]]
    operations: S3SubmissionOperations

    def prepare_stage(self, stage: str) -> None:
        """Clear this stage's stale remote terminal markers before qsub."""
        if self.args.dry_run:
            print(f"[dry-run] Would clear stale .pbs/{stage}.done|failed markers")
            return
        self.operations.clear_stage_markers(
            self.access,
            self.args.artifact_uri,
            self.args.run_id,
            stage,
        )

    def record_job(self, stage: str, job_id: str | None) -> None:
        """Record submitted jobs in rollback and watch/overlap-detection state."""
        if not job_id:
            return
        self.submitted_jobs.append((stage, job_id))
        submission_dir = getattr(self.args, "pbs_submission_dir", None)
        if submission_dir is not None:
            self.operations.persist_job_id(Path(submission_dir), stage, job_id)
