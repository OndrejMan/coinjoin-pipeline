"""Validation and shell rendering for S3-compatible artifact transport."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from urllib.parse import urlparse

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_artifact_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError("artifact URI must use s3:// and include a bucket")
    if any(char.isspace() for char in uri):
        raise ValueError("artifact URI must not contain whitespace")
    return uri.rstrip("/")


def validate_run_id(run_id: str) -> str:
    if len(run_id) > 63 or ".." in run_id or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run ID must be at most 63 characters, match [A-Za-z0-9][A-Za-z0-9._-]*, and must not contain '..'"
        )
    return run_id


def validate_s3_endpoint_url(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("S3 endpoint URL must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("S3 endpoint URL must not contain credentials, query, or fragment")
    return endpoint_url.rstrip("/")


def validate_credentials_file(path: str) -> str:
    if not Path(path).is_absolute():
        raise ValueError("S3 credentials file must be an absolute path")
    return path


def validate_s3_profile(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise ValueError("S3 profile must match [A-Za-z0-9][A-Za-z0-9._-]*")
    return profile


def render_s5cmd_check() -> str:
    return 'command -v s5cmd >/dev/null || { echo "s5cmd is required" >&2; exit 1; }'


def _prefix() -> str:
    return (
        "env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN "
        "-u AWS_PROFILE -u AWS_DEFAULT_PROFILE -u AWS_REGION -u AWS_DEFAULT_REGION "
        's5cmd --credentials-file "$S3_CREDENTIALS_FILE" '
        '--profile "$S3_PROFILE" --endpoint-url "$S3_ENDPOINT_URL"'
    )


def render_s5cmd_sync(source_expr: str, destination_expr: str) -> str:
    return f"{_prefix()} sync {source_expr} {destination_expr}"


def render_s5cmd_cp(source_expr: str, destination_expr: str) -> str:
    return f"{_prefix()} cp {source_expr} {destination_expr}"


def shell_assignment(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value)}"
