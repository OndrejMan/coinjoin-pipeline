"""Atomic, redacted host-side research manifest handling."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from typing import Any

from . import MANIFEST_SCHEMA_VERSION, __version__


SENSITIVE = re.compile(r"(token|password|secret|credential|private[_-]?key)", re.I)


def redact(value: Any, key: str = "") -> Any:
    if SENSITIVE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(redact(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def initial_manifest(**values: Any) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "cli_version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "prepared",
        **values,
    }


def mark_finished(manifest: dict[str, Any], exit_code: int) -> None:
    manifest.update({
        "finished_at": datetime.now(UTC).isoformat(),
        "exit_code": exit_code,
        "status": "completed" if exit_code == 0 else "failed",
    })


def finish_manifest(path: Path, manifest: dict[str, Any], exit_code: int) -> None:
    mark_finished(manifest, exit_code)
    atomic_write(path, manifest)
