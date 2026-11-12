"""S3 run targets, their validation, and the frontend's s5cmd access to them.

Marker waiting lives in ``execution/s3_markers.py`` and exporter staging in
``execution/s3_staging.py``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urlparse

from exporters.artifact_paths import pbs_marker

from coinjoin_pipeline.configuration import required
from coinjoin_pipeline.storage.s5client import FRONTEND_S5CMD_VERSION, S5Client, S5Error, no_object_found, older_s5cmd

if TYPE_CHECKING:
    from coinjoin_pipeline.configuration import PipelineConfiguration

PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The variables the in-pod s5 wrapper (k8s_s3_access.sh) unsets, as a test checks;
# S5Client drops every AWS_* variable.
AWS_SCRUB_VARIABLES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


class ArtifactTransportError(RuntimeError):
    """Raised when S3-compatible artifact transport fails on the frontend."""


@dataclass(frozen=True)
class S3Access:
    """Frontend s5cmd access parameters for one S3-compatible endpoint."""

    endpoint_url: str
    credentials_file: str
    profile: str


@dataclass(frozen=True)
class S3Target:
    """Identity of one S3-backed run and the access that reaches it.

    The three layers each have their own job: S3Target names the run's prefixes, S3Access
    holds the endpoint and credentials, and storage/s5client.S5Client runs s5cmd. A PBS job
    ships s5client.py alone, so S5Client cannot depend on these frontend types.
    """

    artifact_uri: str
    run_id: str
    access: S3Access

    @property
    def run_prefix(self) -> str:
        """Where this run's markers, staged exporters and outputs live."""
        return run_prefix_uri(self.artifact_uri, self.run_id)

    @property
    def log_prefix(self) -> str:
        """Where this run's logs live."""
        return f"{self.run_prefix}/logs"

    @classmethod
    def from_args(cls, args: "PipelineConfiguration") -> "S3Target":
        """Build a target after the public CLI has validated its S3 options."""
        return cls(
            artifact_uri=required(args.artifacts.uri, "--artifact-uri"),
            run_id=required(args.run_id, "--run-id"),
            access=S3Access(
                endpoint_url=required(args.artifacts.endpoint_url, "--s3-endpoint-url"),
                credentials_file=required(args.artifacts.credentials_file, "--s3-credentials-file"),
                profile=required(args.artifacts.profile, "--s3-profile"),
            ),
        )


def validate_artifact_uri(uri: str | None) -> str:
    if not uri:
        raise ValueError("--artifact-uri is required")
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError("artifact URI must use s3:// and include a bucket")
    if any(char.isspace() for char in uri):
        raise ValueError("artifact URI must not contain whitespace")
    return uri.rstrip("/")


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


def s5_client(access: S3Access) -> S5Client:
    return S5Client(access.endpoint_url, access.credentials_file, access.profile)


T = TypeVar("T")


def _s5(access: S3Access, call: Callable[[S5Client], T]) -> T:
    """call with an S5Client for access; a missing s5cmd or a failed listing raises ArtifactTransportError."""
    try:
        return call(s5_client(access))
    except FileNotFoundError as error:
        raise ArtifactTransportError("s5cmd is required on the frontend for S3-compatible transfers") from error
    except S5Error as error:
        raise ArtifactTransportError(str(error)) from error


def run_s5cmd(access: S3Access, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _s5(access, lambda client: client.run(*arguments))


def run_prefix_uri(artifact_uri: str, run_id: str) -> str:
    """The S3 prefix holding every artifact and marker of one run."""
    return f"{artifact_uri}/{run_id}"


def stage_marker_uri(run_prefix: str, stage: str, outcome: str) -> str:
    """``<run-prefix>/.pbs/<stage>.<outcome>``: the terminal marker a PBS stage's trap uploads."""
    return f"{run_prefix}/{pbs_marker(stage, outcome)}"


def s3_listing(access: S3Access, pattern: str) -> list[dict[str, object]]:
    """The objects ``ls`` lists for pattern, empty when none; any other failure raises."""
    return _s5(access, lambda client: client.listing(pattern, timeout=None))


def s3_object_exists(access: S3Access, object_uri: str) -> bool:
    """Whether exactly this key exists: ``ls`` of a key also lists every longer key it starts."""
    return _s5(access, lambda client: client.exists(object_uri, timeout=None))


def s3_prefix_has_objects(access: S3Access, prefix: str) -> bool:
    """Whether any object lies below ``prefix/``."""
    return bool(s3_listing(access, f"{prefix}/*"))


def clear_s3_stage_markers(access: S3Access, run_prefix: str, stage: str) -> None:
    """Clear stale completion markers before submitting the stage that owns them.

    One rm of both markers, an absent one included, and one listing that checks none is left.
    """
    markers = [stage_marker_uri(run_prefix, stage, outcome) for outcome in ("failed", "done")]
    result = run_s5cmd(access, "rm", *markers)
    if result.returncode != 0 and not no_object_found(result):
        raise ArtifactTransportError(
            f"s5cmd rm of the {stage} markers failed (exit {result.returncode}): {(result.stderr or '').strip()}"
        )
    listed = {str(entry.get("key")) for entry in s3_listing(access, stage_marker_uri(run_prefix, stage, "*"))}
    left = sorted(listed & set(markers))
    if left:
        raise ArtifactTransportError(f"S3 markers still exist after deletion: {', '.join(left)}")


def require_s5cmd_version() -> None:
    """Reject a frontend s5cmd too old for `sync --exclude`, which stages the exporters.

    Only the frontend binary is checked here: the uploader runs a pinned image and a PBS
    node its own s5cmd.
    """
    found = older_s5cmd(FRONTEND_S5CMD_VERSION)
    if found is not None:
        raise ArtifactTransportError(
            f"s5cmd on the frontend is too old for this pipeline: found {found}, need 2.1 or newer "
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
    if result.returncode != 0 and not no_object_found(result):
        raise ArtifactTransportError(
            f"S3 access preflight failed for {artifact_uri} (exit {result.returncode}): {(result.stderr or '').strip()}"
        )


def ensure_empty_run_prefix(access: S3Access, artifact_uri: str, run_id: str) -> None:
    """Reject every occupied run prefix, including marker-less partial runs."""
    run_prefix = run_prefix_uri(artifact_uri, run_id)
    if s3_prefix_has_objects(access, run_prefix):
        raise ArtifactTransportError(f"run prefix {run_prefix}/ already contains artifacts; choose a fresh --run-id")
