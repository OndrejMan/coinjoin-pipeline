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

from exporters.artifact_paths import KUBERNETES_MARKER_DIR, pbs_marker

from coinjoin_pipeline.configuration import required
from coinjoin_pipeline.storage.s5client import S5Client, S5Error, no_object_found

if TYPE_CHECKING:
    from coinjoin_pipeline.configuration import PipelineConfiguration

RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
RUN_ID_MAX_LENGTH = 63
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
    """Identity of one S3-backed run and the credentials used to reach it."""

    artifact_uri: str
    run_id: str
    endpoint_url: str
    credentials_file: str
    profile: str

    @property
    def run_prefix(self) -> str:
        """Where this run's markers, staged exporters and outputs live."""
        return run_prefix_uri(self.artifact_uri, self.run_id)

    @property
    def log_prefix(self) -> str:
        """Where this run's logs live."""
        return f"{self.run_prefix}/logs"

    @property
    def access(self) -> S3Access:
        """Return the frontend transport credentials for this run target."""
        return S3Access(
            endpoint_url=self.endpoint_url,
            credentials_file=self.credentials_file,
            profile=self.profile,
        )

    @classmethod
    def from_args(cls, args: "PipelineConfiguration") -> "S3Target":
        """Build a target after the public CLI has validated its S3 options."""
        return cls(
            artifact_uri=required(args.artifacts.uri, "--artifact-uri"),
            run_id=required(args.run_id, "--run-id"),
            endpoint_url=required(args.artifacts.endpoint_url, "--s3-endpoint-url"),
            credentials_file=required(args.artifacts.credentials_file, "--s3-credentials-file"),
            profile=required(args.artifacts.profile, "--s3-profile"),
        )


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


def upload_marker_uri(run_prefix: str, outcome: str) -> str:
    """``<run-prefix>/.k8s/upload.<outcome>``: written by the Kubernetes uploader after the emulation."""
    return f"{run_prefix}/{KUBERNETES_MARKER_DIR}/upload.{outcome}"


def s3_listing(access: S3Access, pattern: str) -> list[dict[str, object]]:
    """The objects ``ls`` lists for pattern, empty when none; any other failure raises."""
    return _s5(access, lambda client: client.listing(pattern, timeout=None))


def s3_object_exists(access: S3Access, object_uri: str) -> bool:
    """Whether exactly this key exists: ``ls`` of a key also lists every longer key it starts."""
    return _s5(access, lambda client: client.exists(object_uri, timeout=None))


def s3_prefix_has_objects(access: S3Access, prefix: str) -> bool:
    """Whether any object lies below ``prefix/``."""
    return bool(s3_listing(access, f"{prefix}/*"))


def delete_s3_object_if_present(access: S3Access, object_uri: str) -> None:
    """Strictly remove one object while treating an absent object as success."""
    if not s3_object_exists(access, object_uri):
        return
    result = run_s5cmd(access, "rm", object_uri)
    if result.returncode != 0:
        raise ArtifactTransportError(
            f"s5cmd rm {object_uri} failed (exit {result.returncode}): {(result.stderr or '').strip()}"
        )
    if s3_object_exists(access, object_uri):
        raise ArtifactTransportError(f"S3 object still exists after deletion: {object_uri}")


def clear_s3_stage_markers(access: S3Access, run_prefix: str, stage: str) -> None:
    """Clear stale completion markers before submitting the stage that owns them."""
    delete_s3_object_if_present(access, stage_marker_uri(run_prefix, stage, "failed"))
    delete_s3_object_if_present(access, stage_marker_uri(run_prefix, stage, "done"))


# `sync --exclude` (used when staging the exporters) landed in s5cmd 2.1.
MINIMUM_S5CMD_VERSION = (2, 1)
S5CMD_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def s5cmd_version() -> tuple[int, ...] | None:
    """Best-effort frontend s5cmd version; None when it cannot be determined."""
    try:
        result = subprocess.run(
            ["s5cmd", "version"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError:
        return None
    match = S5CMD_VERSION_RE.search(result.stdout or "")
    return tuple(int(part) for part in match.groups()) if match else None


def require_s5cmd_version(minimum: tuple[int, ...] = MINIMUM_S5CMD_VERSION) -> None:
    """Reject an s5cmd too old for the flags this client relies on.

    Only the frontend binary is checked: the uploader runs a pinned image and a PBS
    node its own s5cmd. An unparsable version passes, so a vendored build cannot block
    a run it would have handled fine.
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
    if result.returncode != 0 and not no_object_found(result):
        raise ArtifactTransportError(
            f"S3 access preflight failed for {artifact_uri} (exit {result.returncode}): {(result.stderr or '').strip()}"
        )


def ensure_empty_run_prefix(access: S3Access, artifact_uri: str, run_id: str) -> None:
    """Reject every occupied run prefix, including marker-less partial runs."""
    run_prefix = run_prefix_uri(artifact_uri, run_id)
    if s3_prefix_has_objects(access, run_prefix):
        raise ArtifactTransportError(f"run prefix {run_prefix}/ already contains artifacts; choose a fresh --run-id")
