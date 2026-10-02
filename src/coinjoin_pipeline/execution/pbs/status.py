"""PBS status transport shared by orchestration and log watching."""

from __future__ import annotations

import re
import subprocess


class PBSQueryError(RuntimeError):
    """The scheduler could not provide a conclusive response."""


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
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return parse_qstat(result.stdout)
        detail = (result.stderr or result.stdout or "").strip()
        if "unknown job" in detail.lower() or "job has finished" in detail.lower():
            return None
        errors.append(detail)
    detail = next((error for error in errors if error), "scheduler query failed")
    raise PBSQueryError(f"cannot inspect PBS job {job_id}: {detail}")
