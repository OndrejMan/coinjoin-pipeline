"""Run-directory discovery and host-pinned run-id handling."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from exporters.artifact_paths import EMULATION_MARKERS

from coinjoin_pipeline.storage.s3 import validate_run_id


def is_run_dir(path: Path, marker_files: tuple[str, ...] = EMULATION_MARKERS) -> bool:
    return path.is_dir() and any((path / marker).exists() for marker in marker_files)


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


def detect_active_run(emulation_logs_dir: Path, marker_files: tuple[str, ...] = EMULATION_MARKERS) -> Path | None:
    """The run the host pinned through PIPELINE_RUN_ID, once the emulator has stored it."""
    expected_run_id = pipeline_run_id_env()
    if not expected_run_id:
        return None
    run_dir = (emulation_logs_dir / expected_run_id).resolve()
    return run_dir if is_run_dir(run_dir, marker_files) else None
