"""Run manifest construction."""

from __future__ import annotations

import os
from pathlib import Path

from exporters.common import (
    JsonObject,
    first_present,
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
    coinjoin_type = detector.coinjoin_type
    # Every caller passes --engine (default $COINJOIN_ENGINE); a direct call without it guesses.
    inferred_engine = engine or ("joinmarket" if coinjoin_type == "joinmarket" else "wasabi")
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
        },
    }
