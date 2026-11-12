"""The S3 PBS job that parses an emulation with BlockSci, detects CoinJoins and can write the report."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from exporters.artifact_paths import (
    ANALYSIS_ARTIFACT_FILE,
    BASELINE_FILE,
    BITCOIN_DATA_DIR,
    BLOCKSCI_ANALYSIS_DIR,
    COINJOIN_ANALYSIS_DIR,
    EMULATOR_DIR,
    EXPORTED_BLOCK_GLOB,
    EXPORTED_BLOCKS_DIR,
    LOGS_DIR,
    REPORT_DIR,
    REPORT_FILE,
    STAGED_EXPORTERS_DIR,
    block_height_from_path,
)

from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import BlocksciSettings


def emulator_max_block(stage: Stage) -> int:
    """The highest block_<height>.json the emulator exported, which bounds the parser."""
    heights = [
        height
        for path in (stage.work / EXPORTED_BLOCKS_DIR).glob(EXPORTED_BLOCK_GLOB)
        if (height := block_height_from_path(path)) is not None
    ]
    if not heights:
        raise StageFailure("BlockSci parsing could not determine the exported maximum block")
    return max(heights)


def emulator_bitcoin_datadir(stage: Stage) -> Path:
    """The emulator uploads bitcoin_data/ as the datadir itself or with the datadir under data/."""
    datadir = stage.work / BITCOIN_DATA_DIR
    if not (datadir / "regtest/blocks").is_dir() and (datadir / "data/regtest/blocks").is_dir():
        datadir = datadir / "data"
    if not (datadir / "regtest/blocks").is_dir():
        raise StageFailure(
            "BlockSci requires a Bitcoin datadir containing regtest/blocks; checked "
            f"{stage.work}/{BITCOIN_DATA_DIR} and {stage.work}/{BITCOIN_DATA_DIR}/data"
        )
    return datadir


def blocksci(stage: Stage) -> None:
    """Parse the emulated chain, detect CoinJoins and, with the baseline at hand, write the report."""
    settings = cast(BlocksciSettings, stage.settings)
    for name in (BITCOIN_DATA_DIR, EMULATOR_DIR, STAGED_EXPORTERS_DIR):
        stage.download(name)
    datadir = emulator_bitcoin_datadir(stage)
    if settings["include_report"]:
        stage.download(COINJOIN_ANALYSIS_DIR)
        # Retain prior-report provenance.
        stage.download_optional(REPORT_DIR, stage.work / REPORT_DIR)
        stage.require_file(BASELINE_FILE, f"BlockSci S3-compatible reporting requires {BASELINE_FILE}")
    max_block = emulator_max_block(stage)
    stage.singularity(
        f'cd "{CONTAINER_RUNS_ROOT}/$PBS_RUN_ID" && EXPORTED_MAX_BLOCK="$PBS_EXPORTED_MAX_BLOCK" && '
        + settings["command"],
        binds=(
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{datadir}:/mnt/data:ro",
            f"{stage.work / STAGED_EXPORTERS_DIR}:/mnt/exporters:ro",
        ),
        environment=(f"PBS_RUN_ID={stage.run_id}", f"PBS_EXPORTED_MAX_BLOCK={max_block}"),
        cleanenv=True,
    )
    outputs = (
        (REPORT_DIR, REPORT_FILE, settings["include_report"], "BlockSci S3-compatible reporting"),
        (BLOCKSCI_ANALYSIS_DIR, ANALYSIS_ARTIFACT_FILE, settings["export_analysis"], "BlockSci analysis"),
    )
    for _, relative, wanted, producer in outputs:
        if wanted:
            stage.require_file(relative, f"{producer} did not produce {relative}")
    for directory, _, wanted, _ in reversed(outputs):
        if wanted:
            stage.upload(stage.work / directory, directory)
    stage.storage.run("sync", f"{stage.work / LOGS_DIR}/", f"{stage.settings['log_prefix']}/", check=True, timeout=None)
