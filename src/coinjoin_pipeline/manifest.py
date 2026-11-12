"""Atomic, redacted host-side research manifest handling."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from . import MANIFEST_SCHEMA_VERSION, __version__
from .paths import PROJECT_ROOT

SENSITIVE = re.compile(r"(token|password|secret|credential|private[_-]?key|access[_-]?key)", re.I)
PIPELINE_ENVIRONMENT_PREFIXES = (
    "BLOCKSCI_",
    "COINJOIN_",
    "KUBERNETES_",
    "MAPPINGS_",
    "SAKE_",
    "PBS_",
)


def environment_snapshot(environment: Mapping[str, str]) -> dict[str, str]:
    """Record pipeline environment options, omitting secret-bearing keys."""
    return {
        name: value
        for name, value in environment.items()
        if name.startswith(PIPELINE_ENVIRONMENT_PREFIXES) and not SENSITIVE.search(name)
    }


def redact(value: object, key: str = "") -> object:
    if SENSITIVE.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def atomic_write(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(redact(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def git_output(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def pipeline_source() -> dict[str, object]:
    """The pipeline commit that drives the run, from the tree this CLI runs from.

    A checkout answers for itself, also when it is mounted over the image's copy, so the
    image's commit is used only where there is no `.git`. A checkout git cannot read (git
    missing, another owner) records no commit rather than the image's.
    """
    if (PROJECT_ROOT / ".git").exists():
        commit = (git_output("rev-parse", "HEAD") or "").strip() or None
        status = git_output("status", "--porcelain")
        return {
            "origin": "checkout",
            "commit": commit,
            "dirty": None if status is None else bool(status.strip()),
        }
    commit = os.environ.get("COINJOIN_PIPELINE_GIT_COMMIT") or None
    return {"origin": "image" if commit else "unknown", "commit": commit, "dirty": False if commit else None}


def initial_manifest(**values: object) -> dict[str, object]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        # Tells this invocation's record apart from earlier ones over the same run.
        "launch_id": uuid.uuid4().hex,
        "cli_version": __version__,
        "pipeline_source": pipeline_source(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "prepared",
        **values,
    }


def mark_finished(manifest: dict[str, object], exit_code: int) -> None:
    manifest.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": exit_code,
            "status": "completed" if exit_code == 0 else "failed",
        }
    )
