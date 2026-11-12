"""Validation of run IDs."""

from __future__ import annotations

import re

RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
RUN_ID_MAX_LENGTH = 63


RUN_ID_RULE = (
    "must be at most 63 characters, begin and end with an alphanumeric character, "
    "contain only [A-Za-z0-9._-], and not contain '..'"
)


def is_valid_run_id(run_id: str) -> bool:
    return len(run_id) <= RUN_ID_MAX_LENGTH and ".." not in run_id and RUN_ID_RE.fullmatch(run_id) is not None


def validate_run_id(run_id: str) -> str:
    if not is_valid_run_id(run_id):
        raise ValueError(f"run ID {RUN_ID_RULE}")
    return run_id
