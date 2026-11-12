"""How the PBS scheduler answers the frontend: its time limit and a job it no longer knows."""

from __future__ import annotations

#: How long one qdel may take before the scheduler counts as unavailable.
PBS_COMMAND_TIMEOUT_SECONDS = 30


def job_left_scheduler(detail: str) -> bool:
    """Whether a qdel error says the job is no longer known to PBS (finished or purged)."""
    detail = detail.lower()
    return "unknown job" in detail or "job has finished" in detail
