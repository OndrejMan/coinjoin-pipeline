"""Download a completed unified report from the S3 artifact backend."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse


RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
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


class DownloadError(RuntimeError):
    """Raised when the remote report cannot be downloaded safely."""


def validate_output_directory(output_dir: Path, runs_root: Path) -> Path:
    """Refuse broad or unrecognizable destinations before atomic replacement."""
    expanded = output_dir.expanduser()
    if expanded.is_symlink():
        raise ValueError(
            f"existing report output must not be a symbolic link: {expanded}"
        )
    destination = expanded.resolve()
    protected = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path.cwd().resolve(),
        runs_root.expanduser().resolve(),
    }
    if destination in protected or not destination.name:
        raise ValueError(
            f"refusing unsafe report output directory: {destination}"
        )
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(
                f"existing report output must be a real directory: {destination}"
            )
        if not (destination / "unified_report.json").is_file():
            raise ValueError(
                "refusing to replace an existing directory that is not a "
                f"recognized pipeline report: {destination}"
            )
    return destination


@dataclass(frozen=True)
class S3Access:
    endpoint_url: str
    credentials_file: Path
    profile: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coinjoin-pipeline download-report",
        description="Download a completed S3 unified report without a container.",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--artifact-uri",
        default=os.environ.get("ARTIFACT_URI"),
        help="S3 run root, for example s3://coinjoin-thesis/runs.",
    )
    parser.add_argument(
        "--s3-endpoint-url",
        default=os.environ.get("S3_ENDPOINT_URL"),
        help="S3-compatible HTTP(S) endpoint URL.",
    )
    parser.add_argument(
        "--s3-credentials-file",
        default=os.environ.get("S3_CREDENTIALS_FILE"),
        help="Absolute s5cmd credentials-file path.",
    )
    parser.add_argument(
        "--s3-profile",
        default=os.environ.get("S3_PROFILE"),
        help="Named profile in the s5cmd credentials file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Destination directory (default: "
            "RUNS_ROOT/RUN_ID/coinjoinPipeline_data)."
        ),
    )
    return parser


def _validate_run_id(value: str) -> str:
    if len(value) > 63 or ".." in value or not RUN_ID_RE.fullmatch(value):
        raise ValueError(
            "run ID must be at most 63 characters, begin and end with an "
            "alphanumeric character, contain only [A-Za-z0-9._-], and must "
            "not contain '..'"
        )
    return value


def _validate_artifact_uri(value: str | None) -> str:
    if not value:
        raise ValueError("--artifact-uri is required (or set ARTIFACT_URI)")
    parsed = urlparse(value)
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or any(char.isspace() for char in value)
    ):
        raise ValueError(
            "artifact URI must use s3://, include a bucket, and contain no whitespace"
        )
    return value.rstrip("/")


def _validate_endpoint(value: str | None) -> str:
    if not value:
        raise ValueError("--s3-endpoint-url is required (or set S3_ENDPOINT_URL)")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("S3 endpoint URL must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "S3 endpoint URL must not contain credentials, query, or fragment"
        )
    return value.rstrip("/")


def _validate_access(args: argparse.Namespace) -> S3Access:
    if not args.s3_credentials_file:
        raise ValueError(
            "--s3-credentials-file is required (or set S3_CREDENTIALS_FILE)"
        )
    credentials = Path(args.s3_credentials_file).expanduser()
    if not credentials.is_absolute():
        raise ValueError("S3 credentials file must be an absolute path")
    if not credentials.is_file():
        raise ValueError(f"S3 credentials file not found: {credentials}")
    if not args.s3_profile:
        raise ValueError("--s3-profile is required (or set S3_PROFILE)")
    if not PROFILE_RE.fullmatch(args.s3_profile):
        raise ValueError(
            "S3 profile must match [A-Za-z0-9][A-Za-z0-9._-]*"
        )
    return S3Access(
        endpoint_url=_validate_endpoint(args.s3_endpoint_url),
        credentials_file=credentials,
        profile=args.s3_profile,
    )


def _run_s5cmd(access: S3Access, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in AWS_SCRUB_VARIABLES
    }
    return subprocess.run(
        [
            "s5cmd",
            "--credentials-file",
            str(access.credentials_file),
            "--profile",
            access.profile,
            "--endpoint-url",
            access.endpoint_url,
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _object_exists(access: S3Access, uri: str) -> bool:
    result = _run_s5cmd(access, "ls", uri)
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or "").strip()
    if "no object found" in detail.lower():
        return False
    raise DownloadError(
        f"s5cmd could not inspect {uri} (exit {result.returncode}): {detail}"
    )


def download_report(
    access: S3Access,
    artifact_uri: str,
    run_id: str,
    output_dir: Path,
) -> tuple[Path, Path | None]:
    run_prefix = f"{artifact_uri}/{run_id}"
    failed_marker = f"{run_prefix}/.pbs/unified-report.failed"
    done_marker = f"{run_prefix}/.pbs/unified-report.done"
    if _object_exists(access, failed_marker):
        raise DownloadError(
            "the unified-report PBS stage recorded failure: " + failed_marker
        )
    if not _object_exists(access, done_marker):
        raise DownloadError(
            "the unified report is not complete: missing " + done_marker
        )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    source = f"{run_prefix}/coinjoinPipeline_data/"
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.download-",
        dir=output_dir.parent,
    ) as staging_name:
        staging_dir = Path(staging_name)
        result = _run_s5cmd(access, "sync", source, f"{staging_dir}/")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise DownloadError(
                f"s5cmd report download failed (exit {result.returncode}): {detail}"
            )

        staged_json = staging_dir / "unified_report.json"
        staged_markdown = staging_dir / "unified_report.md"
        if not staged_json.is_file():
            raise DownloadError(
                "download completed but unified_report.json is missing from "
                f"{source}"
            )

        has_markdown = staged_markdown.is_file()
        markdown_report = output_dir / "unified_report.md"
        backup_dir: Path | None = None
        if output_dir.exists() or output_dir.is_symlink():
            backup_dir = Path(
                tempfile.mkdtemp(
                    prefix=f".{output_dir.name}.previous-",
                    dir=output_dir.parent,
                )
            )
            backup_dir.rmdir()
            os.replace(output_dir, backup_dir)
        try:
            os.replace(staging_dir, output_dir)
        except OSError:
            if backup_dir is not None:
                os.replace(backup_dir, output_dir)
            raise
        if backup_dir is not None:
            if backup_dir.is_dir() and not backup_dir.is_symlink():
                shutil.rmtree(backup_dir)
            else:
                backup_dir.unlink()

    json_report = output_dir / "unified_report.json"
    return json_report, markdown_report if has_markdown else None


def main(argv: list[str] | None = None, *, runs_root: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_id = _validate_run_id(args.run_id)
        artifact_uri = _validate_artifact_uri(args.artifact_uri)
        access = _validate_access(args)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if shutil.which("s5cmd") is None:
        print(
            "ERROR: s5cmd is required on the frontend PATH to download reports",
            file=sys.stderr,
        )
        return 2

    root = (runs_root or Path.cwd() / "coinjoin-runs").expanduser().resolve()
    try:
        output_dir = validate_output_directory(
            args.output_dir if args.output_dir else root / run_id / "coinjoinPipeline_data",
            root,
        )
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    try:
        json_report, markdown_report = download_report(
            access, artifact_uri, run_id, output_dir
        )
    except (DownloadError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 5

    print(f"[download-report] JSON: {json_report}")
    if markdown_report is not None:
        print(f"[download-report] Markdown: {markdown_report}")
    else:
        print("[download-report] Markdown report was not present in S3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
