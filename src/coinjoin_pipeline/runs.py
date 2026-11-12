"""Run naming and host-manifest placement."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
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


def scenario_file(value: str, engine: str) -> Path | None:
    """The scenario file execution will read (a path, or a name in the scenario catalogue), else None."""
    from .execution.scenarios import SCENARIOS_ROOT, scenario_source_path

    named = scenario_source_path(value, SCENARIOS_ROOT, engine)
    return named if named.is_file() else None


def run_id_for(config: PipelineConfiguration) -> str:
    if config.run_id:
        return config.run_id
    # Without a scenario the emulator runs its engine's default one.
    scenario_name = "default-joinmarket" if config.engine == "joinmarket" else "overactive-local"
    if config.scenario:
        candidate = scenario_file(config.scenario, config.engine) or Path(config.scenario).expanduser()
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            scenario_name = str(data.get("name") or candidate.stem)
        except (OSError, json.JSONDecodeError):
            scenario_name = candidate.stem
    timestamp = datetime.now(ZoneInfo(config.run_timezone)).strftime("%Y-%m-%d_%H-%M")
    slug = slugify_run_component(
        scenario_name,
        max_length=MAX_RUN_ID_LENGTH - len(timestamp) - 1,
    )
    return f"{timestamp}_{slug}"


def read_host_manifest(target: Path) -> dict[str, object]:
    """The run's manifest, empty when it is missing or unreadable."""
    try:
        existing = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    return existing if isinstance(existing, dict) else {}


def store_host_manifest(target: Path, manifest: dict[str, object]) -> None:
    """Record one invocation in the run's manifest without losing the earlier ones.

    ``host_launcher`` is the latest invocation over the run. A new invocation moves the
    previous record to ``earlier_host_launchers`` (oldest first); an invocation updates
    its own record, found by ``launch_id``, wherever it is by then.
    """
    existing = read_host_manifest(target)
    existing.setdefault("schema_version", 1)
    existing.setdefault(
        "mode",
        "external" if manifest.get("action") == "external-analyze" else "emulator",
    )
    existing.setdefault("run_id", target.parent.name)
    launch_id = manifest.get("launch_id")
    recorded = existing.get("earlier_host_launchers")
    earlier = list(recorded) if isinstance(recorded, list) else []
    position = next(
        (index for index, item in enumerate(earlier) if isinstance(item, dict) and item.get("launch_id") == launch_id),
        None,
    )
    if position is not None:
        # A later invocation took the run over in between; it stays the latest.
        earlier[position] = manifest
    else:
        current = existing.get("host_launcher")
        if isinstance(current, dict) and current.get("launch_id") != launch_id:
            earlier.append(current)
        existing["host_launcher"] = manifest
    if earlier:
        existing["earlier_host_launchers"] = earlier
    atomic_write(target, existing)
