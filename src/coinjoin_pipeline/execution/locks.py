"""Advisory command locks and S3 PBS overlap detection."""

from __future__ import annotations

import atexit
import fcntl
import os
from pathlib import Path
from typing import TextIO

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.pbs.submission import pbs_job_probe
from coinjoin_pipeline.execution.run_context import run_dir_under_root
from coinjoin_pipeline.storage.s3 import PROBE_QUEUED, PROBE_RUNNING, PROBE_UNKNOWN

PBS_SUBMIT_LOCK_NAME = ".pbs-submit.lock"

_LOCK_HANDLES: list[TextIO] = []
_HELD_LOCKS: dict[Path, TextIO] = {}


def acquire_lock(path: Path) -> TextIO:
    """Acquire one non-blocking, process-reentrant advisory lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = path.resolve()
    held = _HELD_LOCKS.get(key)
    if held is not None:
        return held
    try:
        handle = path.open("a+", encoding="utf-8")
    except PermissionError as error:
        raise RuntimeError(
            f"Cannot open the pipeline lock {path}: {error.strerror}. "
            "A runs root written by the old containerized wrapper holds root-owned "
            f"files; take it over with `sudo chown -R $(id -u):$(id -g) {path.parent}` "
            "(or point --runs-root somewhere else)"
        ) from error
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError(f"Another pipeline command holds lock: {path}") from error
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    atexit.register(handle.close)
    _LOCK_HANDLES.append(handle)
    _HELD_LOCKS[key] = handle
    return handle


def close_locks() -> None:
    """Close every lock handle held by this process, ignoring shutdown errors."""
    for handle in _LOCK_HANDLES:
        try:
            handle.close()
        except OSError:
            pass

    _LOCK_HANDLES.clear()
    _HELD_LOCKS.clear()


def pbs_submit_lock_path(logs_root: Path, run_id: str) -> Path:
    """Return the per-run lock serialising decomposed S3 PBS submissions."""
    return logs_root / run_id / PBS_SUBMIT_LOCK_NAME


def command_lock_path(args: PipelineConfiguration, logs_root: Path) -> Path:
    """Return the local/shared advisory lock that protects this command."""
    if args.action == "pbs-from-s3":
        return pbs_submit_lock_path(logs_root, required(args.run_id, "--run-id"))
    if args.run_dir and args.action in {"analyze", "export", "coinjoin-analysis", "mappings"}:
        return run_dir_under_root(args.run_dir, logs_root) / ".research.lock"
    return logs_root / ".pipeline.lock"


def ensure_no_active_s3_pbs_submission(run_dir: Path) -> None:
    """Refuse a second S3 PBS graph while a recorded job is still active."""
    active: list[str] = []
    marker_dir = run_dir / ".pbs"
    for jobid_path in sorted(marker_dir.glob("*.jobid")):
        job_id = jobid_path.read_text(encoding="utf-8").strip()
        if not job_id:
            continue
        state = pbs_job_probe(job_id)()
        if state in (PROBE_QUEUED, PROBE_RUNNING, PROBE_UNKNOWN):
            active.append(f"{jobid_path.stem}={job_id} ({state})")
    if active:
        raise RuntimeError(
            f"An earlier pbs-from-s3 graph for this run is still active or cannot be verified: {', '.join(active)}"
        )
