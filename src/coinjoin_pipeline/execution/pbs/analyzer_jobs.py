"""The S3 PBS jobs that run coinjoin-analysis and the unified report.

None of them touches a BlockSci index; that is ``blocksci_jobs``'.
"""

from __future__ import annotations

from typing import cast

from exporters.artifact_paths import (
    ANALYSIS_ARTIFACT_FILE,
    BASELINE_FILE,
    COINJOIN_ANALYSIS_DIR,
    EMULATOR_DATA_DIR,
    EMULATOR_DIR,
    REPORT_DIR,
    REPORT_FILE,
    STAGED_EXPORTERS_DIR,
)

from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, CONTAINER_SELECTED_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import (
    CommandSettings,
    UnifiedReportSettings,
)


def unified_report(stage: Stage) -> None:
    """Join the precomputed analyzer outputs into the JSON and Markdown report."""
    settings = cast(UnifiedReportSettings, stage.settings)
    stage.log("downloading lightweight report inputs")
    for name in settings["downloads"]:
        stage.download(name)
    for relative in (ANALYSIS_ARTIFACT_FILE, BASELINE_FILE, f"{STAGED_EXPORTERS_DIR}/unified_report.py"):
        stage.require_file(relative, f"Unified S3 report requires {relative}")
    stage.log("assembling JSON and Markdown from precomputed analyzer outputs")
    stage.container(
        CONTAINER_RUNS_ROOT,
        (
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{stage.work / STAGED_EXPORTERS_DIR}:/mnt/exporters:ro",
        ),
        settings["command"],
    )
    stage.require_file(REPORT_FILE, f"Unified S3 report did not produce {REPORT_FILE}")
    stage.upload(stage.work / REPORT_DIR, REPORT_DIR)
    stage.log("report upload complete")


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
