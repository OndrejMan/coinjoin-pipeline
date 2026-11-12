"""PBS status transport shared by orchestration and log watching, and what a job state means."""

from __future__ import annotations

import re
import subprocess

from coinjoin_pipeline.execution.probes import PROBE_QUEUED, PROBE_RUNNING, PROBE_TERMINAL

from .defaults import PBS_ACTIVE_STATES, PBS_QUEUED_STATES, PBS_TERMINAL_STATES

#: How long one qstat or qdel may take before the scheduler counts as unavailable.
PBS_COMMAND_TIMEOUT_SECONDS = 30
#: The state of a job qstat no longer knows: it finished or was purged.
LEFT_SCHEDULER = "MISSING"


class PBSQueryError(RuntimeError):
    """The scheduler could not provide a conclusive response."""


def job_left_scheduler(detail: str) -> bool:
    """Whether a qstat/qdel error says the job is no longer known to PBS (finished or purged)."""
    detail = detail.lower()
    return "unknown job" in detail or "job has finished" in detail


def job_phase(state: str) -> str | None:
    """``PROBE_QUEUED``, ``PROBE_RUNNING`` or ``PROBE_TERMINAL`` for a qstat state; None for an undocumented one.

    Every client of the scheduler decides by this; each chooses what an undocumented state means to it.
    """
    if state == LEFT_SCHEDULER or state in PBS_TERMINAL_STATES:
        return PROBE_TERMINAL
    if state in PBS_QUEUED_STATES:
        return PROBE_QUEUED
    if state in PBS_ACTIVE_STATES:
        return PROBE_RUNNING
    return None


def parse_qstat(payload: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    for line in payload.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*=\s*(.*)$", line)
        if match:
            current = match.group(1)
            fields[current] = match.group(2).strip()
        elif current is not None and line[:1].isspace():
            fields[current] += line.strip()
    return fields


def job_details(job_id: str) -> dict[str, str] | None:
    """Return fields, None for an explicitly missing job, or raise on transport errors."""
    errors = []
    for command in (["qstat", "-x", "-f", job_id], ["qstat", "-f", job_id]):
        try:
            result = subprocess.run(
                command,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=PBS_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{' '.join(command)} timed out after {PBS_COMMAND_TIMEOUT_SECONDS} s")
            continue
        if result.returncode == 0:
            return parse_qstat(result.stdout)
        detail = (result.stderr or result.stdout or "").strip()
        if job_left_scheduler(detail):
            return None
        errors.append(detail)
    detail = next((error for error in errors if error), "scheduler query failed")
    raise PBSQueryError(f"cannot inspect PBS job {job_id}: {detail}")
