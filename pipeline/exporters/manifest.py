"""Run manifest construction."""

from __future__ import annotations

import os
from pathlib import Path

from exporters.common import (
    JsonObject,
    first_present,
    git_commit_for_path,
)
from exporters.parameters import Detector
from exporters.report_types import RunManifest


def build_run_manifest(
    run_dir: Path,
    scenario: JsonObject | None,
    detector: Detector,
    engine: str | None = None,
    emulator_git_commit: str | None = None,
) -> RunManifest:
    exporters_root = Path(__file__).resolve().parent
    coinjoin_type = detector.coinjoin_type
    inferred_engine = engine or os.environ.get("COINJOIN_ENGINE")
    if not inferred_engine:
        inferred_engine = "joinmarket" if coinjoin_type == "joinmarket" else "wasabi"
    return {
        "run_id": run_dir.name,
        "scenario": {
            "name": scenario.get("name") if scenario else None,
            "sha256": scenario.get("sha256") if scenario else None,
        },
        "execution": {
            "engine": inferred_engine,
            "coinjoin_type": coinjoin_type,
            "reproduction_command": os.environ.get("REPRODUCTION_COMMAND"),
        },
        "detector": detector.manifest(),
        "source_commits": {
            "coinjoin_emulator": first_present(
                emulator_git_commit,
                os.environ.get("COINJOIN_EMULATOR_GIT_COMMIT"),
            ),
            "exporters": git_commit_for_path(exporters_root),
        },
    }
