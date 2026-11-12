"""The S3 PBS job that runs coinjoin-analysis.

None of them touches a BlockSci index; that is ``blocksci_jobs``'.
"""

from __future__ import annotations

from typing import cast

from exporters.artifact_paths import (
    BASELINE_FILE,
    COINJOIN_ANALYSIS_DIR,
    EMULATOR_DATA_DIR,
    EMULATOR_DIR,
)

from coinjoin_pipeline.execution.pbs.stage import CONTAINER_SELECTED_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import CommandSettings


def coinjoin_analysis(stage: Stage) -> None:
    """Run the coinjoin-analysis baseline over the emulator's wallet and coordinator logs.

    The collector scans every run below its root, so the container sees a root that
    holds this run alone: the output directory, with the emulator data read-only in it.
    """
    settings = cast(CommandSettings, stage.settings)
    stage.download(EMULATOR_DIR)
    data = stage.work / EMULATOR_DATA_DIR
    if not data.is_dir():
        raise StageFailure(f"Coinjoin analysis requires {EMULATOR_DATA_DIR}")
    output = stage.work / COINJOIN_ANALYSIS_DIR
    output.mkdir(parents=True, exist_ok=True)
    selected = stage.scratch / "coinjoin-analysis-selected"
    (selected / stage.run_id).mkdir(parents=True, exist_ok=True)
    run_root = f"{CONTAINER_SELECTED_ROOT}/{stage.run_id}"
    stage.container(
        CONTAINER_SELECTED_ROOT,
        (
            f"{selected}:{CONTAINER_SELECTED_ROOT}:rw",
            f"{output}:{run_root}:rw",
            f"{data}:{run_root}/data:ro",
        ),
        settings["command"],
    )
    stage.require_file(BASELINE_FILE, f"Coinjoin analysis did not produce {BASELINE_FILE}")
    stage.upload(output, COINJOIN_ANALYSIS_DIR)
