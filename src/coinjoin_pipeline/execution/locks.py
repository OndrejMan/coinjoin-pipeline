"""Advisory command locks."""

from __future__ import annotations

import atexit
import fcntl
import os
from pathlib import Path
from typing import TextIO

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.run_context import run_dir_under_root

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
    _HELD_LOCKS[key] = handle
    return handle


def close_locks() -> None:
    """Close every lock handle held by this process, ignoring shutdown errors."""
    for handle in _HELD_LOCKS.values():
        try:
            handle.close()
        except OSError:
            pass
    _HELD_LOCKS.clear()


def command_lock_path(args: PipelineConfiguration, logs_root: Path) -> Path:
    """Return the local/shared advisory lock that protects this command."""
    if args.run_dir and args.action in {"analyze", "export", "coinjoin-analysis"}:
        return run_dir_under_root(args.run_dir, logs_root) / ".research.lock"
    return logs_root / ".pipeline.lock"
