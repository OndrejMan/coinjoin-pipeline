"""Validation, shell rendering, and frontend polling for S3-compatible artifact transport."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# The exporters ship to the compute node on their own, so the tree hash they
# record in the report has to come from the same implementation the frontend
# logs here — otherwise the two provenance values are not comparable.
from exporters.common import tree_sha256

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

AWS_SCRUB_VARIABLES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)

S3_POLL_INTERVAL_SECONDS = 30
PROBE_RUNNING = "running"
PROBE_TERMINAL = "terminal"
PROBE_UNKNOWN = "unknown"

class ArtifactTransportError(RuntimeError):
    """Raised when S3-compatible artifact transport fails on the frontend."""


@dataclass(frozen=True)
class S3Access:
    """Frontend s5cmd access parameters for one S3-compatible endpoint."""

    endpoint_url: str
    credentials_file: str
    profile: str


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


def render_s5cmd_rm(target_expr: str) -> str:
    """Render deletion of one validated, explicitly constructed object key."""
    return f"{_prefix()} rm {target_expr}"


def shell_assignment(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value)}"


def scrubbed_s3_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in AWS_SCRUB_VARIABLES}


def run_s5cmd(access: S3Access, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = [
        "s5cmd",
        "--credentials-file",
        access.credentials_file,
        "--profile",
        access.profile,
        "--endpoint-url",
        access.endpoint_url,
        *arguments,
    ]
    try:
        return subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=scrubbed_s3_environment(),
        )
    except FileNotFoundError as error:
        raise ArtifactTransportError("s5cmd is required on the frontend for S3-compatible transfers") from error


def s3_object_exists(access: S3Access, object_uri: str) -> bool:
    result = run_s5cmd(access, "ls", object_uri)
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").strip()
    if "no object found" in stderr.lower():
        return False
    raise ArtifactTransportError(f"s5cmd ls {object_uri} failed (exit {result.returncode}): {stderr}")


# `sync --exclude` (used when staging the exporters) landed in s5cmd 2.1.
MINIMUM_S5CMD_VERSION = (2, 1)
S5CMD_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def s5cmd_version() -> tuple[int, ...] | None:
    """Best-effort frontend s5cmd version; None when it cannot be determined."""
    try:
        result = subprocess.run(
            ["s5cmd", "version"], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except OSError:
        return None
    match = S5CMD_VERSION_RE.search(result.stdout or "")
    return tuple(int(part) for part in match.groups()) if match else None


def require_s5cmd_version(minimum: tuple[int, ...] = MINIMUM_S5CMD_VERSION) -> None:
    """Reject an s5cmd too old for the flags this client relies on.

    Only the frontend binary is a variable — the in-cluster and PBS sides use
    pinned images — and an unparsable version is not treated as a failure, so a
    vendored build cannot block a run it would have handled fine.
    """
    version = s5cmd_version()
    if version is not None and version < minimum:
        raise ArtifactTransportError(
            "s5cmd on the frontend is too old for this pipeline: "
            f"found {'.'.join(str(part) for part in version)}, "
            f"need {'.'.join(str(part) for part in minimum)} or newer "
            "(sync --exclude support)"
        )


def s3_access_preflight(access: S3Access, artifact_uri: str) -> None:
    """Fail fast when s5cmd, the credentials file, or the endpoint is unusable."""
    if shutil.which("s5cmd") is None:
        raise ArtifactTransportError("s5cmd is required on the frontend PATH for S3-compatible full-run")
    require_s5cmd_version()
    if not Path(access.credentials_file).is_file():
        raise ArtifactTransportError(f"S3 credentials file not found: {access.credentials_file}")
    result = run_s5cmd(access, "ls", f"{artifact_uri}/*")
    if result.returncode != 0 and "no object found" not in (result.stderr or "").lower():
        raise ArtifactTransportError(
            f"S3 access preflight failed for {artifact_uri} "
            f"(exit {result.returncode}): {(result.stderr or '').strip()}"
        )


def ensure_empty_run_prefix(access: S3Access, artifact_uri: str, run_id: str) -> None:
    """Reject every occupied run prefix, including marker-less partial runs."""
    run_prefix = f"{artifact_uri}/{run_id}"
    if s3_object_exists(access, f"{run_prefix}/*"):
        raise ArtifactTransportError(
            f"run prefix {run_prefix}/ already contains artifacts; choose a fresh --run-id"
        )


# Entry points of the report and BlockSci analysis paths. Both are bind-mounted
# and executed on the compute node, so a staging step that uploads neither is
# useless; checking them turns a late node-side failure into a frontend error.
REQUIRED_EXPORTERS = ("unified_report.py", "blocksci_export/analysis.py")


def ensure_local_exporters(exporters_dir: Path) -> None:
    """Reject an exporter tree that is missing either entry point."""
    for relative in REQUIRED_EXPORTERS:
        candidate = exporters_dir / relative
        if not candidate.is_file():
            raise ArtifactTransportError(f"Required exporter is missing: {candidate}")


STAGED_EXPORTERS_COMPLETE = "complete"
STAGED_EXPORTERS_MISSING = "missing"
STAGED_EXPORTERS_PARTIAL = "partial"


def staged_exporters_state(
    access: S3Access, artifact_uri: str, run_id: str
) -> tuple[str, list[str]]:
    """Classify the exporter tree already staged under a run prefix.

    One entry point is not proof of a usable tree. A prefix staged before the
    ``blocksci/`` → ``blocksci_export/`` rename still carries
    ``unified_report.py``, so a resumed run would clear the frontend check and
    only fail on the compute node, which runs the renamed analysis path. The
    same holds for an upload that died halfway through.
    """
    prefix = f"{artifact_uri}/{run_id}/.pipeline/exporters/"
    missing = [
        relative
        for relative in REQUIRED_EXPORTERS
        if not s3_object_exists(access, f"{prefix}{relative}")
    ]
    if not missing:
        return STAGED_EXPORTERS_COMPLETE, []
    if len(missing) == len(REQUIRED_EXPORTERS):
        return STAGED_EXPORTERS_MISSING, missing
    return STAGED_EXPORTERS_PARTIAL, missing


def upload_exporters(access: S3Access, artifact_uri: str, run_id: str, exporters_dir: Path) -> None:
    """Stage the checkout's exporters into the run prefix before the Job starts.

    Running bare from a checkout means the tree can hold host bytecode; shipping
    a 3.14 ``__pycache__`` into BlockSci's Python 3.8 is at best noise, so the
    sync filters it out rather than snapshotting the tree somewhere first.
    """
    ensure_local_exporters(exporters_dir)
    # Informational provenance for "what exactly was staged": the upload takes
    # the live checkout, uncommitted changes included, and the node has no .git
    # to resolve a commit from. The unified report recomputes this hash from the
    # tree it actually ran, so the two can be compared after the fact.
    print(f"[stage] exporters tree sha256={tree_sha256(exporters_dir)}")
    destination = f"{artifact_uri}/{run_id}/.pipeline/exporters/"
    result = run_s5cmd(
        access,
        "sync",
        "--exclude", "*__pycache__*",
        "--exclude", "*.pyc",
        f"{exporters_dir}/",
        destination,
    )
    if result.returncode != 0:
        raise ArtifactTransportError(
            f"failed to upload exporters to {destination} "
            f"(exit {result.returncode}): {(result.stderr or '').strip()}"
        )
    for relative in REQUIRED_EXPORTERS:
        if not s3_object_exists(access, f"{destination}{relative}"):
            raise ArtifactTransportError(
                f"exporter upload did not produce {destination}{relative}"
            )


def wait_for_s3_marker(
    stage: str,
    done_uri: str,
    failed_uri: str,
    access: S3Access,
    *,
    timeout_seconds: int,
    poll_interval: int = S3_POLL_INTERVAL_SECONDS,
    probe: Callable[[], str] | None = None,
) -> None:
    """Block until the stage uploads its S3 marker, with probe and deadline fallbacks.

    ``probe`` reports remote liveness (``PROBE_RUNNING``/``PROBE_TERMINAL``/
    ``PROBE_UNKNOWN``); after a terminal report one extra poll cycle runs so a
    marker upload that races the probe still wins.
    """
    deadline = time.monotonic() + timeout_seconds
    terminal_seen = False
    while True:
        if s3_object_exists(access, failed_uri):
            raise ArtifactTransportError(f"S3-compatible stage failed: {stage} (marker {failed_uri})")
        if s3_object_exists(access, done_uri):
            return
        # Not probed during the grace cycle: the terminal report already landed.
        probe_state = probe() if probe is not None and not terminal_seen else None
        if time.monotonic() >= deadline:
            if probe_state != PROBE_RUNNING:
                raise ArtifactTransportError(
                    f"Timed out waiting for S3 stage marker: {stage} ({done_uri})"
                )
            # The job is verifiably alive (queued or running); shared-cluster
            # queue time must not be counted against the walltime budget.
            deadline = time.monotonic() + timeout_seconds
            print(
                f"[WARN] {stage} exceeded its {timeout_seconds}s wait budget but the job is "
                f"still alive; extending the deadline.",
                file=sys.stderr,
            )
        if terminal_seen:
            raise ArtifactTransportError(f"S3-compatible stage ended without marker: {stage}")
        if probe_state == PROBE_TERMINAL:
            terminal_seen = True
        time.sleep(poll_interval)
