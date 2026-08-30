"""Run-directory discovery and host-pinned run-id handling."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from client.artifacts import validate_run_id


def is_run_dir(path: Path, marker_files: tuple[str, ...]) -> bool:
    return path.is_dir() and any((path / marker).exists() for marker in marker_files)


def run_dirs(emulation_logs_dir: Path, marker_files: tuple[str, ...]) -> set[Path]:
    if not emulation_logs_dir.exists():
        return set()
    return {
        child.resolve()
        for child in emulation_logs_dir.iterdir()
        if is_run_dir(child, marker_files)
    }


def newest_run_dir(emulation_logs_dir: Path, marker_files: tuple[str, ...]) -> Path | None:
    candidates = run_dirs(emulation_logs_dir, marker_files)
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def pipeline_run_id_env() -> str:
    """Return the host-pinned run id, ignoring an invalid environment value."""
    run_id_value = os.environ.get("PIPELINE_RUN_ID", "")
    if not run_id_value:
        return ""
    try:
        return validate_run_id(run_id_value)
    except ValueError as error:
        print(f"[WARN] Ignoring invalid PIPELINE_RUN_ID: {error}", file=sys.stderr)
        return ""


def detect_active_run(
    emulation_logs_dir: Path,
    before: set[Path],
    marker_files: tuple[str, ...],
) -> Path | None:
    """Locate the newly created run, honouring a host-pinned id first."""
    expected_run_id = pipeline_run_id_env()
    if expected_run_id:
        run_dir = (emulation_logs_dir / expected_run_id).resolve()
        return run_dir if is_run_dir(run_dir, marker_files) else None
    created = sorted(
        run_dirs(emulation_logs_dir, marker_files) - before,
        key=lambda path: path.stat().st_mtime,
    )
    return created[-1] if created else newest_run_dir(emulation_logs_dir, marker_files)


def run_dir_under_root(run_dir_arg: str, runs_root: Path) -> Path:
    """Resolve an existing --run-dir directly below the configured root."""
    requested = Path(run_dir_arg).expanduser()
    resolved = (
        requested.resolve()
        if requested.is_absolute()
        else (runs_root / requested).resolve()
    )
    if resolved.parent != runs_root:
        print(
            f"[ERROR] --run-dir must name a run inside the runs root {runs_root}, "
            f"but {resolved} is outside it.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not resolved.exists():
        print(f"[ERROR] Run directory not found: {resolved}", file=sys.stderr)
        sys.exit(2)
    return resolved


def resolve_run_id(
    run_dir_arg: str | None,
    emulation_logs_dir: Path,
    marker_files: tuple[str, ...],
) -> str | None:
    if run_dir_arg:
        return run_dir_under_root(run_dir_arg, emulation_logs_dir).name
    latest = newest_run_dir(emulation_logs_dir, marker_files)
    if latest is None:
        return None
    print(f"[WARN] No --run-dir provided; using newest run folder: {latest}", file=sys.stderr)
    return latest.name
