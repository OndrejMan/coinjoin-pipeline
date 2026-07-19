"""Run naming and host-manifest placement."""

from __future__ import annotations

from datetime import datetime
import json
import re
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo

from .commands import option_value
from .manifest import atomic_write

# Mirrors the run-id validation in coinjoin-emulator's manager CLI.
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def valid_run_id(run_id: str) -> bool:
    return len(run_id) <= 63 and ".." not in run_id and RUN_ID_PATTERN.fullmatch(run_id) is not None


def run_id_for(arguments: list[str]) -> str:
    explicit_run_id = option_value(arguments, "--run-id")
    if explicit_run_id:
        return explicit_run_id

    timezone = option_value(arguments, "--run-timezone") or "Europe/Prague"
    scenario_arg = option_value(arguments, "--scenario")
    engine = option_value(arguments, "--engine") or "wasabi"
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
    return f"{timestamp}_{scenario_name}"


def manifest_target(
    action: str, arguments: list[str], runs_root: Path, run_id: str | None = None,
) -> Path | None:
    run_dir = option_value(arguments, "--run-dir")
    if run_dir:
        target = Path(run_dir).expanduser()
        if not target.is_absolute():
            target = runs_root / target
        return target / "research_manifest.json"
    if action in {"full-run", "recreate"}:
        return runs_root / (run_id or run_id_for(arguments)) / "research_manifest.json"
    return None


def store_host_manifest(target: Path, manifest: dict[str, object]) -> None:
    try:
        existing = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["host_launcher"] = manifest
    atomic_write(target, existing)
