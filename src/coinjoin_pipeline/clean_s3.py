"""Delete pipeline artifacts from the S3 backend (destructive maintenance)."""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from .download_report import (
    RUN_ID_RE,
    S3Access,
    _run_s5cmd,
    _validate_access,
    _validate_artifact_uri,
)


class CleanError(RuntimeError):
    """Raised when the remote objects cannot be cleaned safely."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coinjoin-pipeline clean-s3",
        description=(
            "Delete every object under an S3 run root (or a single run). "
            "This is irreversible."
        ),
    )
    parser.add_argument(
        "--artifact-uri",
        default=os.environ.get("ARTIFACT_URI"),
        help="S3 run root to clean, for example s3://xman-coinjoin/runs.",
    )
    parser.add_argument(
        "--run-id",
        help="Restrict deletion to a single run under the artifact URI.",
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
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be deleted without removing anything.",
    )
    return parser


def _validate_run_id(value: str) -> str:
    if len(value) > 63 or ".." in value or not RUN_ID_RE.fullmatch(value):
        raise ValueError(
            "run ID must be at most 63 characters, match "
            "[A-Za-z0-9][A-Za-z0-9._-]*, and must not contain '..'"
        )
    return value


def _target_prefix(artifact_uri: str, run_id: str | None) -> str:
    if run_id is None:
        return artifact_uri
    return f"{artifact_uri}/{run_id}"


def list_objects(access: S3Access, prefix: str) -> list[str]:
    """Return the object URIs living under ``prefix`` (recursively)."""
    result = _run_s5cmd(access, "ls", f"{prefix}/*")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "no object found" in detail.lower():
            return []
        raise CleanError(
            f"s5cmd could not inspect {prefix} (exit {result.returncode}): {detail}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def delete_prefix(access: S3Access, prefix: str) -> None:
    """Recursively delete every object under ``prefix``."""
    result = _run_s5cmd(access, "rm", f"{prefix}/*")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "no object found" in detail.lower():
            return
        raise CleanError(
            f"s5cmd delete failed for {prefix} (exit {result.returncode}): {detail}"
        )


def _confirm(prefix: str, count: int) -> bool:
    if not sys.stdin.isatty():
        print(
            "ERROR: refusing to delete without --yes on a non-interactive stdin",
            file=sys.stderr,
        )
        return False
    print(f"About to delete {count} object(s) under {prefix}")
    print("This cannot be undone. Type the prefix to confirm:")
    try:
        answer = input("> ").strip()
    except EOFError:
        return False
    if answer != prefix:
        print("Confirmation did not match; aborting.", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None, *, runs_root: object = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        artifact_uri = _validate_artifact_uri(args.artifact_uri)
        run_id = _validate_run_id(args.run_id) if args.run_id else None
        access = _validate_access(args)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if shutil.which("s5cmd") is None:
        print(
            "ERROR: s5cmd is required on the frontend PATH to clean S3",
            file=sys.stderr,
        )
        return 2

    prefix = _target_prefix(artifact_uri, run_id)
    try:
        objects = list_objects(access, prefix)
    except CleanError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 5

    if not objects:
        print(f"[clean-s3] Nothing to delete under {prefix}")
        return 0

    if args.dry_run:
        print(f"[clean-s3] Would delete {len(objects)} object(s) under {prefix}:")
        for uri in objects:
            print(f"  {uri}")
        return 0

    if not args.yes and not _confirm(prefix, len(objects)):
        return 3

    try:
        delete_prefix(access, prefix)
    except CleanError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 5

    print(f"[clean-s3] Deleted {len(objects)} object(s) under {prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
