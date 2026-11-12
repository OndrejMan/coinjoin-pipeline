"""Scenario discovery and host/container scenario paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from coinjoin_pipeline.execution import settings
from coinjoin_pipeline.paths import SCENARIOS_ROOT as SCENARIO_FILES

SCENARIOS_ROOT = Path(os.environ.get("SCENARIOS_ROOT", SCENARIO_FILES)).expanduser()


def scenario_source_path(value: str | None, scenarios_dir: Path, engine: str = settings.DEFAULT_ENGINE) -> Path:
    """Resolve an explicit host file or a name inside the default catalogue."""
    if not value:
        return host_scenario_path(default_container_scenario(engine), scenarios_dir)
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    if candidate.is_absolute():
        return candidate
    parts = candidate.parts
    named = scenarios_dir.joinpath(*(parts[1:] if parts and parts[0] == "scenarios" else parts))
    if named.suffix != ".json":
        named = named.with_suffix(".json")
    return named.resolve()


def default_container_scenario(engine: str) -> str:
    return (
        settings.DEFAULT_JOINMARKET_CONTAINER_SCENARIO
        if engine == "joinmarket"
        else settings.DEFAULT_CONTAINER_SCENARIO
    )


def container_scenario_path(scenario: str | None, scenarios_dir: Path, engine: str = settings.DEFAULT_ENGINE) -> str:
    if not scenario:
        return default_container_scenario(engine)
    scenario_path = Path(scenario).expanduser()
    if not scenario_path.is_absolute():
        parts = scenario_path.parts
        if len(parts) >= 1 and parts[0] == "scenarios":
            scenario_path = scenarios_dir.joinpath(*parts[1:])
        else:
            scenario_path = scenarios_dir / scenario_path
    scenario_path = scenario_path.resolve()
    try:
        relative_path = scenario_path.relative_to(scenarios_dir.resolve())
    except ValueError:
        print(
            f"[ERROR] Scenario {scenario_path} is outside the scenarios directory {scenarios_dir}, so it cannot be mounted into the container. Copy it under {scenarios_dir} and pass it by name.",
            file=sys.stderr,
        )
        sys.exit(2)
    return settings.CONTAINER_SCENARIOS_DIR + "/" + str(relative_path).replace(os.sep, "/")


def host_scenario_path(container_scenario: str, scenarios_dir: Path) -> Path:
    """Map a container scenario path back to its host path, preserving nesting."""
    relative_path = container_scenario.removeprefix(settings.CONTAINER_SCENARIOS_DIR + "/")
    return scenarios_dir.joinpath(*relative_path.split("/"))
