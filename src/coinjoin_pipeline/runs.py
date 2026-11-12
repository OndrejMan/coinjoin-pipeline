"""Run naming."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .configuration import PipelineConfiguration
from .run_ids import RUN_ID_MAX_LENGTH


def slugify_run_component(value: str, *, max_length: int) -> str:
    """Return a lowercase ASCII run-ID component with alphanumeric edges."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "scenario"


def scenario_file(value: str | None, engine: str) -> Path | None:
    """The scenario file execution will read (a path, a name in the catalogue, or the engine's default), else None."""
    from .execution.scenarios import scenario_source_path
    from .paths import scenarios_dir

    named = scenario_source_path(value, scenarios_dir(), engine)
    return named if named.is_file() else None


def run_id_for(config: PipelineConfiguration) -> str:
    if config.run_id:
        return config.run_id
    # Without a scenario the emulator runs its engine's default one (execution/settings.py).
    candidate = scenario_file(config.scenario, config.engine)
    if candidate is None:
        candidate = Path(config.scenario or "scenario").expanduser()
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
        scenario_name = str(data.get("name") or candidate.stem)
    except (OSError, json.JSONDecodeError):
        scenario_name = candidate.stem
    # Seconds, so two launches of one scenario within a minute get different IDs.
    timestamp = datetime.now(ZoneInfo(config.run_timezone)).strftime("%Y-%m-%d_%H-%M-%S")
    slug = slugify_run_component(
        scenario_name,
        max_length=RUN_ID_MAX_LENGTH - len(timestamp) - 1,
    )
    return f"{timestamp}_{slug}"
