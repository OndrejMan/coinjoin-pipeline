"""Download a completed unified report from the S3 artifact backend."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from exporters.artifact_paths import REPORT_DIR, REPORT_JSON

from .storage.s3 import (
    ArtifactTransportError,
    S3Access,
    access_from_values,
    run_s5cmd,
    s3_object_exists,
    validate_artifact_uri,
    validate_run_id,
)


class DownloadError(RuntimeError):
    """Raised when the remote report cannot be downloaded safely."""


def validate_output_directory(output_dir: Path, runs_root: Path) -> Path:
    """Refuse broad or unrecognizable destinations before atomic replacement."""
    expanded = output_dir.expanduser()
    if expanded.is_symlink():
        raise ValueError(f"existing report output must not be a symbolic link: {expanded}")
    destination = expanded.resolve()
    protected = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path.cwd().resolve(),
        runs_root.expanduser().resolve(),
    }
    if destination in protected or not destination.name:
        raise ValueError(f"refusing unsafe report output directory: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"existing report output must be a real directory: {destination}")
        if not (destination / REPORT_JSON).is_file():
            raise ValueError(
                f"refusing to replace an existing directory that is not a recognized pipeline report: {destination}"
            )
    return destination


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
        help=("Destination directory (default: RUNS_ROOT/RUN_ID/coinjoinPipeline_data)."),
    )
    return parser


def download_report(
    access: S3Access,
    artifact_uri: str,
    run_id: str,
    output_dir: Path,
) -> tuple[Path, Path | None]:
    run_prefix = f"{artifact_uri}/{run_id}"
    failed_marker = f"{run_prefix}/.pbs/unified-report.failed"
    done_marker = f"{run_prefix}/.pbs/unified-report.done"
    if s3_object_exists(access, failed_marker):
        raise DownloadError("the unified-report PBS stage recorded failure: " + failed_marker)
    if not s3_object_exists(access, done_marker):
        raise DownloadError("the unified report is not complete: missing " + done_marker)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    source = f"{run_prefix}/{REPORT_DIR}/"
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.download-",
        dir=output_dir.parent,
    ) as staging_name:
        staging_dir = Path(staging_name)
        result = run_s5cmd(access, "sync", source, f"{staging_dir}/")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise DownloadError(f"s5cmd report download failed (exit {result.returncode}): {detail}")

        staged_json = staging_dir / REPORT_JSON
        staged_markdown = staging_dir / "unified_report.md"
        if not staged_json.is_file():
            raise DownloadError(f"download completed but {REPORT_JSON} is missing from {source}")

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

    json_report = output_dir / REPORT_JSON
    return json_report, markdown_report if has_markdown else None


def main(argv: list[str] | None = None, *, runs_root: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_id = validate_run_id(args.run_id)
        artifact_uri = validate_artifact_uri(args.artifact_uri)
        access = access_from_values(
            args.s3_endpoint_url,
            args.s3_credentials_file,
            args.s3_profile,
            check_file=True,
        )
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
            args.output_dir if args.output_dir else root / run_id / REPORT_DIR,
            root,
        )
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    try:
        json_report, markdown_report = download_report(access, artifact_uri, run_id, output_dir)
    except (DownloadError, ArtifactTransportError, OSError) as error:
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
