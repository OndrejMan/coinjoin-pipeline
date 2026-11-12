"""S3 marker waiting and best-effort PBS cancellation for one S3 run."""

from __future__ import annotations

import errno
import sys
import time
from collections.abc import Callable

from coinjoin_pipeline.execution.pbs.submission import pbs_job_probe, qdel_pbs_job
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pbs_settings import pbs_wait_timeout
from coinjoin_pipeline.execution.probes import PROBE_QUEUED, PROBE_RUNNING, PROBE_TERMINAL
from coinjoin_pipeline.storage.s3 import ArtifactTransportError, S3Access, s3_object_exists, stage_marker_uri

S3_POLL_INTERVAL_SECONDS = 30
# A frontend at its per-user task limit (MetaCentrum caps user cgroups at pids.max)
# briefly cannot spawn s5cmd or kubectl; that says nothing about the remote stage.
TRANSIENT_SPAWN_ERRNOS = frozenset({errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE})


def wait_for_s3_marker(
    stage: str,
    done_uri: str,
    failed_uri: str,
    access: S3Access,
    *,
    timeout_seconds: int,
    start_timeout_seconds: int | None = None,
    poll_interval: int = S3_POLL_INTERVAL_SECONDS,
    probe: Callable[[], str] | None = None,
) -> None:
    """Block until the stage uploads its S3 marker, with probe and deadline fallbacks.

    ``probe`` reports remote state (``PROBE_QUEUED``/``PROBE_RUNNING``/
    ``PROBE_TERMINAL``/``PROBE_UNKNOWN``). Queue time may extend the start
    deadline when ``start_timeout_seconds`` is omitted (the PBS queueing
    contract), but callers can supply a finite scheduling deadline. The
    execution deadline starts once the job is observed running and is never
    extended. After a terminal report one extra poll cycle runs so a marker
    upload that races the probe still wins.
    """
    start_budget = timeout_seconds if start_timeout_seconds is None else start_timeout_seconds
    start_deadline = time.monotonic() + start_budget
    execution_deadline: float | None = None
    terminal_seen = False
    while True:
        try:
            if s3_object_exists(access, failed_uri):
                raise ArtifactTransportError(f"S3-compatible stage failed: {stage} (marker {failed_uri})")
            if s3_object_exists(access, done_uri):
                return
            # Not probed during the grace cycle: the terminal report already landed.
            probe_state = probe() if probe is not None and not terminal_seen else None
        except OSError as error:
            if error.errno not in TRANSIENT_SPAWN_ERRNOS:
                raise
            if time.monotonic() >= (execution_deadline if execution_deadline is not None else start_deadline):
                raise ArtifactTransportError(f"Timed out waiting for S3 stage marker: {stage} ({done_uri})") from error
            print(f"[WARN] {stage} poll could not run ({error}); retrying in {poll_interval}s", file=sys.stderr)
            time.sleep(poll_interval)
            continue
        now = time.monotonic()
        if probe_state == PROBE_RUNNING and execution_deadline is None:
            execution_deadline = now + timeout_seconds
        deadline = execution_deadline if execution_deadline is not None else start_deadline
        if now >= deadline:
            if execution_deadline is not None or probe_state != PROBE_QUEUED or start_timeout_seconds is not None:
                raise ArtifactTransportError(f"Timed out waiting for S3 stage marker: {stage} ({done_uri})")
            # Shared-cluster queue time must not consume the execution budget.
            start_deadline = now + timeout_seconds
            print(
                f"[WARN] {stage} exceeded its {timeout_seconds}s start budget but the job "
                f"is still queued; extending the start deadline.",
                file=sys.stderr,
            )
        if terminal_seen:
            raise ArtifactTransportError(f"S3-compatible stage ended without marker: {stage}")
        if probe_state == PROBE_TERMINAL:
            terminal_seen = True
        time.sleep(poll_interval)


def wait_for_s3_pbs_stage(*, stage: str, job_id: str, run_prefix: str, access: S3Access, walltime: str) -> None:
    """Wait for one PBS stage using the common S3 marker/probe contract."""
    print(f"[full-run] Waiting for {stage} marker (PBS job {job_id})")
    wait_for_s3_marker(
        stage,
        stage_marker_uri(run_prefix, stage, "done"),
        stage_marker_uri(run_prefix, stage, "failed"),
        access,
        timeout_seconds=pbs_wait_timeout(walltime),
        probe=pbs_job_probe(job_id),
    )


def _cancel(log_prefix: str, stage: str, job_id: str) -> bool:
    """qdel one job; a scheduler or spawn error counts as not cancelled, reported with the stage."""
    try:
        return qdel_pbs_job(job_id)
    except (OSError, PBSError, RuntimeError) as error:
        print(f"[{log_prefix}] Could not cancel {stage} job {job_id}: {error}", file=sys.stderr)
        return False


def cancel_dependent_pbs_job(stage_name: str, job_id: str) -> bool:
    """Cancel a dependent stage after an upstream wait failed, and say so.

    Reports the recovery command when qdel is missing or refused, so the
    operator is never told a job was cancelled when it is still queued.
    """
    print(
        f"[full-run] Cancelling dependent {stage_name} PBS job {job_id}",
        file=sys.stderr,
    )
    cancelled = _cancel("full-run", stage_name, job_id)
    if not cancelled:
        print(
            f"[full-run] {stage_name} PBS job {job_id} may still be queued or running; cancel it with: qdel {job_id}",
            file=sys.stderr,
        )
    return cancelled


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
