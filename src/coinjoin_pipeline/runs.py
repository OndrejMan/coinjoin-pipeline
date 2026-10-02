"""Run naming and host-manifest placement."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo

from .configuration import PipelineConfiguration
from .manifest import atomic_write

MAX_RUN_ID_LENGTH = 63


def slugify_run_component(value: str, *, max_length: int) -> str:
    """Return a lowercase ASCII run-ID component with alphanumeric edges."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "scenario"


def run_id_for(config: PipelineConfiguration) -> str:
    explicit_run_id = config.run_id
    if explicit_run_id:
        return explicit_run_id

    timezone = config.run_timezone or "Europe/Prague"
    scenario_arg = config.scenario
    engine = config.engine or "wasabi"
    scenario_name = "default-joinmarket" if engine == "joinmarket" else "overactive-local"
    if scenario_arg:
        candidate = Path(scenario_arg).expanduser()
        if not candidate.is_file():
            resource = files("coinjoin_pipeline").joinpath(f"resources/scenarios/{candidate.name}")
            if resource.is_file():
                candidate = Path(str(resource))
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            scenario_name = str(data.get("name") or candidate.stem)
        except (OSError, json.JSONDecodeError):
            scenario_name = candidate.stem
    timestamp = datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d_%H-%M")
    slug = slugify_run_component(
        scenario_name,
        max_length=MAX_RUN_ID_LENGTH - len(timestamp) - 1,
    )
    return f"{timestamp}_{slug}"


def store_host_manifest(target: Path, manifest: dict[str, object]) -> None:
    try:
        existing = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing.setdefault("schema_version", 1)
    existing.setdefault(
        "mode",
        "external" if manifest.get("action") == "external analyze" else "emulator",
    )
    existing.setdefault("run_id", target.parent.name)
    existing["host_launcher"] = manifest
    atomic_write(target, existing)
