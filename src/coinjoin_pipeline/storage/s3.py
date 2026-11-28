"""S3 run targets, their validation, and the frontend's s5cmd access to them.

Marker waiting lives in ``execution/s3_markers.py`` and exporter staging in
``execution/s3_staging.py``.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urlparse

from exporters.artifact_paths import KUBERNETES_MARKER_DIR, PBS_MARKER_DIR, RUN_MANIFEST, pbs_marker

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
    # A fixed prefix shared by successive runs (the persistent index jobs) in place of
    # <artifact_uri>/<run_id>; each run replaces its state there and keeps its logs apart.
    state_prefix: str | None = None

    @property
    def run_prefix(self) -> str:
        """Where this run's markers, staged exporters and outputs live."""
        return self.state_prefix or run_prefix_uri(self.artifact_uri, self.run_id)

    @property
    def log_prefix(self) -> str:
        """Where this run's logs live; under a fixed prefix every run keeps its own."""
        return f"{self.run_prefix}/logs/{self.run_id}" if self.state_prefix else f"{self.run_prefix}/logs"

    @classmethod
    def from_args(cls, args: "PipelineConfiguration") -> "S3Target":
        """Build a target after the public CLI has validated its S3 options."""
        return cls(
            artifact_uri=required(args.artifacts.uri, "artifacts.uri"),
            run_id=required(args.run_id, "run_id"),
            access=S3Access(
                endpoint_url=required(args.artifacts.endpoint_url, "artifacts.endpoint_url"),
                credentials_file=required(args.artifacts.credentials_file, "artifacts.credentials_file"),
                profile=required(args.artifacts.profile, "artifacts.profile"),
            ),
            state_prefix=args.blocksci.index_state_uri,
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


def s3_key_pair(credentials_file: str, profile: str) -> tuple[str, str]:
    """The access key ID and secret of one profile of an s5cmd/AWS credentials file."""
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(credentials_file):
        raise ArtifactTransportError(f"S3 credentials file not found: {credentials_file}")
    if not config.has_section(profile):
        raise ArtifactTransportError(f"S3 credentials file {credentials_file} has no profile {profile}")
    section = config[profile]
    key_id, secret = section.get("aws_access_key_id", "").strip(), section.get("aws_secret_access_key", "").strip()
    if not key_id or not secret:
        raise ArtifactTransportError(f"S3 profile {profile} in {credentials_file} lacks an access key or its secret")
    return key_id, secret


def validate_s3_profile(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise ValueError("S3 profile must match [A-Za-z0-9][A-Za-z0-9._-]*")
    return profile


def access_from_values(
    endpoint_url: str | None,
    credentials_file: str | None,
    profile: str | None,
    *,
    check_file: bool = False,
) -> S3Access:
    """Normalize frontend credentials once, optionally requiring a local file."""
    if not endpoint_url:
        raise ValueError("--s3-endpoint-url is required")
    if not credentials_file:
        raise ValueError("--s3-credentials-file is required")
    if not profile:
        raise ValueError("--s3-profile is required")
    credentials = Path(credentials_file).expanduser()
    validate_credentials_file(str(credentials))
    if check_file and not credentials.is_file():
        raise ValueError(f"S3 credentials file not found: {credentials}")
    return S3Access(
        validate_s3_endpoint_url(endpoint_url),
        str(credentials),
        validate_s3_profile(profile),
    )


def add_s3_arguments(parser: argparse.ArgumentParser, uri_help: str) -> None:
    """The S3 options of the frontend utilities, defaulting to the environment."""
    parser.add_argument("--artifact-uri", default=os.environ.get("ARTIFACT_URI"), help=uri_help)
    parser.add_argument(
        "--s3-endpoint-url", default=os.environ.get("S3_ENDPOINT_URL"), help="S3-compatible HTTP(S) endpoint URL."
    )
    parser.add_argument(
        "--s3-credentials-file",
        default=os.environ.get("S3_CREDENTIALS_FILE"),
        help="Absolute s5cmd credentials-file path.",
    )
    parser.add_argument(
        "--s3-profile", default=os.environ.get("S3_PROFILE"), help="Named profile in the s5cmd credentials file."
    )


def s3_arguments_access(args: argparse.Namespace) -> tuple[str, S3Access]:
    """Validated artifact URI and access of ``add_s3_arguments``; ValueError when unusable."""
    if not args.artifact_uri:
        # Only the utilities read the environment; the main CLI takes the option or YAML alone.
        raise ValueError("--artifact-uri is required (or set ARTIFACT_URI)")
    artifact_uri = validate_artifact_uri(args.artifact_uri)
    access = access_from_values(args.s3_endpoint_url, args.s3_credentials_file, args.s3_profile, check_file=True)
    if shutil.which("s5cmd") is None:
        raise ValueError("s5cmd is required on the frontend PATH")
    return artifact_uri, access


def shell_assignment(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value)}"


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


def run_manifest_uri(run_prefix: str) -> str:
    """``<run-prefix>/research_manifest.json``: the frontend's launch record, copied beside the artifacts."""
    return f"{run_prefix}/{RUN_MANIFEST}"


def s3_listing(access: S3Access, pattern: str) -> list[dict[str, object]]:
    """The objects ``ls`` lists for pattern, empty when none; any other failure raises."""
    return _s5(access, lambda client: client.listing(pattern, timeout=None))


def s3_object_exists(access: S3Access, object_uri: str) -> bool:
    """Whether exactly this key exists: ``ls`` of a key also lists every longer key it starts."""
    return _s5(access, lambda client: client.exists(object_uri, timeout=None))


def s3_prefix_has_objects(access: S3Access, prefix: str) -> bool:
    """Whether any object lies below ``prefix/``."""
    return bool(s3_listing(access, f"{prefix}/*"))


def s3_json(access: S3Access, object_uri: str) -> dict[str, object] | None:
    """The JSON object stored at exactly this key, None when the key does not exist."""
    if not s3_object_exists(access, object_uri):
        return None
    return _s5(access, lambda client: client.cat_json(object_uri))


def upload_s3_file(access: S3Access, source: Path, object_uri: str, *, timeout: float | None = None) -> None:
    """Copy one local file to ``object_uri``, replacing what is there."""
    try:
        result = _s5(access, lambda client: client.run("cp", source, object_uri, timeout=timeout))
    except subprocess.TimeoutExpired as error:
        raise ArtifactTransportError(f"s5cmd cp to {object_uri} did not finish in {timeout:g} s") from error
    if result.returncode != 0:
        raise ArtifactTransportError(
            f"s5cmd cp to {object_uri} failed (exit {result.returncode}): {(result.stderr or '').strip()}"
        )


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


def upload_s3_job_id(access: S3Access, run_prefix: str, stage: str, record: Path) -> None:
    """Copy a stage's local ``.jobid`` record to ``.pbs/<stage>.jobid`` in the run prefix.

    The S3 copy lets any frontend find the run's jobs after the local run directory is gone.
    """
    uri = stage_marker_uri(run_prefix, stage, "jobid")
    result = run_s5cmd(access, "cp", str(record), uri)
    if result.returncode != 0:
        raise ArtifactTransportError(
            f"s5cmd cp of the {stage} job ID failed (exit {result.returncode}): {(result.stderr or '').strip()}"
        )


def s3_job_ids(access: S3Access, run_prefix: str) -> dict[str, str]:
    """The job IDs the run recorded in its S3 prefix, by stage; empty when it recorded none."""
    jobs: dict[str, str] = {}
    marker_root = f"{run_prefix}/{PBS_MARKER_DIR}/"
    for entry in s3_listing(access, stage_marker_uri(run_prefix, "*", "jobid")):
        key = str(entry.get("key", ""))
        if not key.startswith(marker_root) or not key.endswith(".jobid"):
            continue
        stage = key[len(marker_root) : -len(".jobid")]
        result = run_s5cmd(access, "cat", key)
        if result.returncode != 0:
            raise ArtifactTransportError(f"s5cmd cat {key} failed (exit {result.returncode})")
        if stage and "/" not in stage and result.stdout.strip():
            jobs[stage] = result.stdout.strip()
    return jobs


def require_s5cmd_version() -> None:
    """Reject a frontend s5cmd too old for `sync --exclude`, which stages the exporters.

    Only the frontend binary is checked here: the uploader runs a pinned image, and a PBS
    job checks its node's s5cmd before it streams an archive through `pipe`.
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
