"""The S3 PBS jobs that write a BlockSci index: the combined run and parse.

The job that reads an index is ``blocksci_analysis_jobs``; ``blocksci_index`` holds
what both share.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from exporters.artifact_paths import (
    ANALYSIS_ARTIFACT_FILE,
    BASELINE_FILE,
    BITCOIN_DATA_DIR,
    BLOCKSCI_ANALYSIS_DIR,
    BLOCKSCI_CONFIG_FILE,
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
    INDEX_FILES,
    canonicalize_index,
    config_value,
    require_tools,
    run_index,
)
from coinjoin_pipeline.execution.pbs.index_store import index_manifest
from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import (
    BlocksciParseSettings,
    BlocksciSettings,
    IndexWriterSettings,
    SourceSettings,
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


class WrittenIndex:
    """The index a parse or combined job writes.

    It is written in the job's scratch and published as its single backup in
    <run prefix>/blocksci-parse_data, with its manifest and file list.
    """

    def __init__(self, stage: Stage) -> None:
        self.stage = stage
        # Fail before parsing: the job ends by packing the run's index.
        require_tools("zstd")
        self.store = run_index(stage, f"{stage.run_prefix}/{BLOCKSCI_PARSE_DIR}")

    def publish(self, source_kind: str, network: str, max_block: int, extra: dict[str, object]) -> None:
        """Record the finished index with the resources of this job, then make it available.

        The backup replaces its previous manifest first, so a failed upload leaves no index
        that looks complete.
        """
        stage = self.stage
        extra = {**extra, "pbs_resources": cast(IndexWriterSettings, stage.settings)["pbs_resources"]}
        manifest = index_manifest(stage.run_id, stage.settings["image_name"], source_kind, network, max_block, extra)
        stage.log("archiving reusable index")
        self.store.publish(stage.work / BLOCKSCI_CONFIG_FILE, manifest)
        self.store.backup(threads=stage.settings["ncpus"])
        stage.log("reusable cache upload complete")


def blocksci(stage: Stage) -> None:
    """Parse the emulated chain, detect CoinJoins and, with the baseline at hand, write the report.

    The parsed index is published like the reusable workflow's, so cached jobs can reuse it.
    """
    settings = cast(BlocksciSettings, stage.settings)
    index = WrittenIndex(stage)
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
    index.publish("emulator", "bitcoin_regtest", max_block, {})
    for directory, _, wanted, _ in reversed(outputs):
        if wanted:
            stage.upload(stage.work / directory, directory)
    stage.storage.run("sync", f"{stage.work / LOGS_DIR}/", f"{stage.settings['log_prefix']}/", check=True, timeout=None)


def run_parser(stage: Stage, command: str, datadir: Path, max_block: int, message: str) -> None:
    """Run the job's parse command over datadir inside the BlockSci image.

    Like the combined job, it runs without the host environment: a module the PBS node
    loaded (PYTHONPATH, LD_LIBRARY_PATH) must not reach the image's Python.
    """
    stage.log(message)
    stage.singularity(
        f'cd "{CONTAINER_RUNS_ROOT}/$PBS_RUN_ID" && {command}',
        binds=(
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{stage.work / STAGED_EXPORTERS_DIR}:/mnt/exporters:ro",
            f"{datadir}:/mnt/data:ro",
        ),
        environment=(f"PBS_RUN_ID={stage.run_id}", f"PBS_EXPORTED_MAX_BLOCK={max_block}"),
        cleanenv=True,
    )


def parse_source(stage: Stage, source: SourceSettings) -> tuple[Path | None, int]:
    """Prepare the parse's single source: its datadir (None for a copied index) and height."""
    kind = source["kind"]
    stage.log(f"preparing {source['description']}")
    if kind == "external-bitcoin":
        datadir = Path(str(source["datadir"]))
        if not (datadir / "blocks").is_dir():
            raise StageFailure(f"External Bitcoin coin directory must contain blocks/: {datadir}")
        return datadir, int(source["max_block"])
    if kind == "external-blocksci":
        index = Path(str(source["blocksci_dir"]))
        for relative in ("config.json", "parsed/chain/block.dat"):
            if not (index / relative).is_file():
                raise StageFailure(f"External BlockSci directory must contain {relative}: {index}")
        # Only the index itself: its backup holds config.json and parsed/, and files.txt lists them.
        imported = stage.work / BLOCKSCI_DIR
        imported.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(index / "config.json", imported / "config.json")
        shutil.copytree(index / "parsed", imported / "parsed")
        canonicalize_index(stage)
        limit = config_value(stage, "parser", "maxBlockNum")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise StageFailure("External BlockSci config has no positive parser.maxBlockNum")
        max_block = limit - 1
        stage.log(f"imported external index through block {max_block}")
        return None, max_block
    stage.download(BITCOIN_DATA_DIR)
    stage.download(EXPORTED_BLOCKS_DIR)
    return emulator_bitcoin_datadir(stage), emulator_max_block(stage)


def blocksci_parse(stage: Stage) -> None:
    """Parse once and publish a checksummed reusable index."""
    settings = cast(BlocksciParseSettings, stage.settings)
    index = WrittenIndex(stage)
    stage.download(STAGED_EXPORTERS_DIR)
    source = settings["source"]
    datadir, max_block = parse_source(stage, source)
    if datadir is not None:
        run_parser(
            stage,
            settings["command"],
            datadir,
            max_block,
            f"parsing {source['description']} through block {max_block}",
        )
    for relative in INDEX_FILES:
        stage.require_file(relative, f"BlockSci parser did not produce {relative}")
    index.publish(source["kind"], source["network"], max_block, {})
