"""Validation of the S3 options of a run."""

from __future__ import annotations

import re
from urllib.parse import urlparse

RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
RUN_ID_MAX_LENGTH = 63


def validate_artifact_uri(uri: str | None) -> str:
    if not uri:
        raise ValueError("--artifact-uri is required (or set ARTIFACT_URI)")
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError("artifact URI must use s3:// and include a bucket")
    if any(char.isspace() for char in uri):
        raise ValueError("artifact URI must not contain whitespace")
    return uri.rstrip("/")


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


def validate_s3_endpoint_url(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("S3 endpoint URL must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("S3 endpoint URL must not contain credentials, query, or fragment")
    return endpoint_url.rstrip("/")
