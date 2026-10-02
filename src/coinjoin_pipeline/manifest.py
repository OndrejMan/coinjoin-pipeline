"""Atomic, redacted host-side research manifest handling."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from . import MANIFEST_SCHEMA_VERSION, __version__

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
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def atomic_write(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(redact(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def initial_manifest(**values: object) -> dict[str, object]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "cli_version": __version__,
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
