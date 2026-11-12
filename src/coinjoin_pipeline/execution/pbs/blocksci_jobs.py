"""The S3 PBS jobs that write a BlockSci index: the combined run and parse.

The job that reads an index is ``blocksci_analysis_jobs``; ``blocksci_index`` holds
what both share.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from exporters.artifact_paths import (
    ANALYSIS_ARTIFACT_FILE,
    BASELINE_FILE,
    BITCOIN_DATA_DIR,
    BLOCKSCI_ANALYSIS_DIR,
    BLOCKSCI_DIR,
    BLOCKSCI_PARSE_DIR,
    COINJOIN_ANALYSIS_DIR,
    EMULATOR_DIR,
    EXPORTED_BLOCK_GLOB,
    EXPORTED_BLOCKS_DIR,
    LOGS_DIR,
    MAPPINGS_DIR,
    REPORT_DIR,
    REPORT_FILE,
    STAGED_EXPORTERS_DIR,
    block_height_from_path,
)

from coinjoin_pipeline.execution.pbs.blocksci_index import (
    INDEX_ARCHIVE,
    INDEX_FILES,
    require_tools,
)
from coinjoin_pipeline.execution.pbs.index_store import index_manifest
from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import (
    BlocksciSettings,
    CommandSettings,
)


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


def publish_index(stage: Stage, source_kind: str, network: str, max_block: int, extra: dict[str, object]) -> None:
    """Stream the run's index to S3; its checksum sidecar and then its manifest make it valid.

    The previous manifest goes first, so a failed upload leaves no index that looks complete.
    """
    prefix = f"{stage.run_prefix}/{BLOCKSCI_PARSE_DIR}"
    archive = f"{prefix}/{INDEX_ARCHIVE}"
    stage.log("archiving reusable index")
    stage.storage.remove(f"{prefix}/manifest.json")
    checksum, _ = stage.storage.upload_archive(stage.work, [BLOCKSCI_DIR], archive, stage.settings["ncpus"])
    cache = stage.work / BLOCKSCI_PARSE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    sidecar = cache / f"{INDEX_ARCHIVE}.sha256"
    sidecar.write_text(f"{checksum}  {INDEX_ARCHIVE}\n")
    stage.storage.copy(sidecar, f"{archive}.sha256")
    manifest = index_manifest(
        stage.run_id,
        stage.settings["image_name"],
        source_kind,
        network,
        max_block,
        {**extra, "archive": INDEX_ARCHIVE, "archive_sha256": checksum},
    )
    (cache / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    stage.storage.copy(cache / "manifest.json", f"{prefix}/manifest.json")


def blocksci(stage: Stage) -> None:
    """Parse the emulated chain, detect CoinJoins and, with the baseline at hand, write the report."""
    settings = cast(BlocksciSettings, stage.settings)
    for name in (BITCOIN_DATA_DIR, EMULATOR_DIR, STAGED_EXPORTERS_DIR):
        stage.download(name)
    datadir = emulator_bitcoin_datadir(stage)
    if settings["include_report"]:
        stage.download(COINJOIN_ANALYSIS_DIR)
        # Retain optional mappings and prior-report provenance without fetching an index.
        for name in (MAPPINGS_DIR, REPORT_DIR):
            stage.download_optional(name, stage.work / name)
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


def run_parser(stage: Stage, command: str, datadir: Path, max_block: int, message: str) -> None:
    """Run the job's parse command over datadir inside the BlockSci image."""
    stage.log(message)
    stage.singularity(
        f'cd "{CONTAINER_RUNS_ROOT}/$PBS_RUN_ID" && {command}',
        binds=(
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{stage.work / STAGED_EXPORTERS_DIR}:/mnt/exporters:ro",
            f"{datadir}:/mnt/data:ro",
        ),
        environment=(f"PBS_RUN_ID={stage.run_id}", f"PBS_EXPORTED_MAX_BLOCK={max_block}"),
    )


def blocksci_parse(stage: Stage) -> None:
    """Parse once and publish a checksummed reusable index."""
    settings = cast(CommandSettings, stage.settings)
    require_tools("zstd")
    stage.download(STAGED_EXPORTERS_DIR)
    stage.download(BITCOIN_DATA_DIR)
    stage.download(EXPORTED_BLOCKS_DIR)
    max_block = emulator_max_block(stage)
    run_parser(
        stage,
        settings["command"],
        emulator_bitcoin_datadir(stage),
        max_block,
        f"parsing emulator Bitcoin and exported-block inputs through block {max_block}",
    )
    for relative in INDEX_FILES:
        stage.require_file(relative, f"BlockSci parser did not produce {relative}")
    publish_index(stage, "emulator", "bitcoin_regtest", max_block, {})
    stage.log("reusable cache upload complete")
