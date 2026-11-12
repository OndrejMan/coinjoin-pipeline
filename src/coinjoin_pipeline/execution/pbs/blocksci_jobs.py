"""The S3 PBS jobs that write a BlockSci index: the combined run, parse, update and backup.

The job that reads an index is ``blocksci_analysis_jobs``; ``blocksci_index`` holds
what both share.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
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
    INDEX_ARCHIVE,
    INDEX_FILES,
    canonicalize_index,
    check_manifest,
    container_run,
    open_index,
    require_tools,
    restore_index,
)
from coinjoin_pipeline.execution.pbs.index_store import IndexStore, index_manifest
from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import (
    BlocksciParseSettings,
    BlocksciSettings,
    BlocksciUpdateSettings,
    PersistentJobSettings,
    PersistentSettings,
    SourceSettings,
)


# An update's copy of the manifest of the index it extends; job scratch, never published.
SOURCE_CACHE_DIR = "source-blocksci-parse_data"


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
    """Parse the emulated chain, detect CoinJoins and, with the baseline at hand, write the report.

    The parsed index is published like the reusable workflow's, so cached jobs can reuse it.
    """
    settings = cast(BlocksciSettings, stage.settings)
    require_tools("zstd")
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
    publish_index(stage, "emulator", "bitcoin_regtest", max_block, {})
    for directory, _, wanted, _ in reversed(outputs):
        if wanted:
            stage.upload(stage.work / directory, directory)
    stage.storage.run("sync", f"{stage.work / LOGS_DIR}/", f"{stage.settings['log_prefix']}/", check=True, timeout=None)


def set_max_block(stage: Stage, max_block: int) -> None:
    """Let the parser read through max_block: BlockSci's maxBlockNum is exclusive."""
    config = stage.work / BLOCKSCI_CONFIG_FILE
    text, replaced = re.subn(
        r'("maxBlockNum"\s*:\s*)-?\d+', lambda match: f"{match.group(1)}{max_block + 1}", config.read_text()
    )
    if not replaced:
        raise StageFailure("Could not update parser.maxBlockNum")
    config.write_text(text)


def check_source(manifest: dict, source_kind: str, network: str) -> None:
    """An update resumes only an index parsed from the same kind of source and network."""
    if manifest.get("source_kind") != source_kind:
        raise StageFailure(f"Incremental update requires a {source_kind} cache")
    if manifest.get("network") != network:
        raise StageFailure(f"Source cache network does not match {network}")


def require_persistent(settings: PersistentJobSettings) -> PersistentSettings:
    persistent = settings["persistent"]
    if not persistent:
        raise StageFailure("This job has no persistent index")
    return persistent


def publish_persistent(
    stage: Stage,
    settings: BlocksciParseSettings,
    index: IndexStore,
    max_block: int,
    extra: dict[str, object],
) -> None:
    """Record the parsed index in place, publish its height to the run and unlock."""
    stage.log(f"recording persistent index at height {max_block}")
    manifest = index_manifest(
        stage.run_id,
        stage.settings["image_name"],
        settings["source"]["kind"],
        settings["source"]["network"],
        max_block,
        {**extra, "pbs_resources": settings["pbs_resources"]},
    )
    index.publish(stage.work / BLOCKSCI_CONFIG_FILE, manifest)
    # The lab cannot see the multijob scratch; it reads the index height from here.
    stage.storage.copy(index.path / "manifest.json", f"{stage.run_prefix}/blocksci-index-manifest.json")
    index.unlock()


def run_parser(
    stage: Stage, command: str, datadir: Path, max_block: int, message: str, index_binds: Sequence[str] = ()
) -> None:
    """Run the job's parse command over datadir inside the BlockSci image."""
    stage.log(message)
    stage.singularity(
        f'cd "{CONTAINER_RUNS_ROOT}/$PBS_RUN_ID" && {command}',
        binds=(
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{stage.work / STAGED_EXPORTERS_DIR}:/mnt/exporters:ro",
            f"{datadir}:/mnt/data:ro",
            *index_binds,
        ),
        environment=(f"PBS_RUN_ID={stage.run_id}", f"PBS_EXPORTED_MAX_BLOCK={max_block}"),
    )


def verify_chain(
    stage: Stage, settings: BlocksciParseSettings, max_block: int, *, update: bool, index_binds: Sequence[str]
) -> tuple[int, str]:
    """Check the parsed chain against the block archive before anything publishes it."""
    source = settings["source"]
    stage.log("[block-archive] verifying the parsed chain before cache publication")
    run_directory = container_run(stage)
    arguments = [
        f"{run_directory}/{BLOCKSCI_CONFIG_FILE}",
        "--max-block",
        str(max_block),
        "--network",
        str(source["network"]),
        *(["--expected-block-hash", str(source["expected_hash"])] if source.get("expected_hash") else []),
        *(["--source-manifest", f"{run_directory}/{SOURCE_CACHE_DIR}/manifest.json"] if update else []),
        "--output",
        f"{run_directory}/{BLOCKSCI_DIR}/chain-verification.json",
    ]
    python = shlex.split(settings["python_command"])
    command = stage.image_command(
        "env",
        *python,
        "/mnt/exporters/verify_chain.py",
        *arguments,
        binds=(
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{stage.work / STAGED_EXPORTERS_DIR}:/mnt/exporters:ro",
            *index_binds,
        ),
    )
    subprocess.run(command, check=True)
    verified = json.loads((stage.work / BLOCKSCI_DIR / "chain-verification.json").read_text())
    return int(verified["exported_max_block"]), str(verified["exported_block_hash"])


def download_block_archive(
    stage: Stage, uri: str, max_block: int, source_manifest: Path | None = None
) -> tuple[Path, dict]:
    """Fetch only the archive files below uri a parse, or an update of source_manifest, needs.

    Returns the block directory's datadir and the cache manifest fields that let a later
    update resume.
    """
    datadir = stage.work / BITCOIN_DATA_DIR
    work = stage.work / "block-archive"
    for directory in (datadir / "blocks", work / "sidecars"):
        directory.mkdir(parents=True, exist_ok=True)
    stage.storage.copy(f"{uri}/blk*.dat.json", f"{work / 'sidecars'}/", timeout=None)
    # The job ships it at its place in the package, as it is on the frontend.
    helper = [sys.executable, str(Path(__file__).parents[1] / "block_archive.py")]
    update = ["--source-manifest", str(source_manifest)] if source_manifest is not None else []
    commands, fields, packed = work / "download.s5cmd", work / "manifest-fields.json", work / "packed.txt"
    # A file with both forms is mid-packing: only the raw object is known to be verified.
    stored = {str(entry["key"]).rsplit("/", 1)[-1] for entry in stage.storage.listing(f"{uri}/*", timeout=None)}
    packed.write_text(
        "".join(
            name[: -len(".zst")] + "\n"
            for name in sorted(stored)
            if name.endswith(".dat.zst") and name[:-4] not in stored
        )
    )
    subprocess.run(
        [
            *helper,
            "select",
            str(work / "sidecars"),
            str(datadir / "blocks"),
            "--uri",
            uri,
            "--max-block",
            str(max_block),
            *update,
            "--packed",
            str(packed),
            "--commands",
            str(commands),
            "--manifest-fields",
            str(fields),
        ],
        check=True,
    )
    stage.storage.run("run", str(commands), check=True, timeout=None)
    subprocess.run([*helper, "unpack", str(datadir / "blocks")], check=True)
    subprocess.run([*helper, "verify", str(datadir / "blocks"), "--max-block", str(max_block), *update], check=True)
    # The helper writes the fields as they follow another manifest field.
    return datadir, json.loads("{" + fields.read_text().strip().lstrip(",") + "}")


def parse_source(stage: Stage, source: SourceSettings) -> tuple[Path | None, int, dict]:
    """Prepare the parse's single source: its datadir (None for a copied index), height and manifest fields."""
    kind = source["kind"]
    stage.log(f"preparing {source['description']}")
    if kind == "external-bitcoin":
        datadir = Path(str(source["datadir"]))
        if not (datadir / "blocks").is_dir():
            raise StageFailure(f"External Bitcoin coin directory must contain blocks/: {datadir}")
        return datadir, int(source["max_block"]), {}
    if kind == "bitcoin-blocks-s3":
        datadir, extra = download_block_archive(stage, source["blocks_uri"], int(source["max_block"]))
        return datadir, int(source["max_block"]), extra
    if kind == "external-blocksci":
        index = Path(str(source["blocksci_dir"]))
        for relative in ("config.json", "parsed/chain/block.dat"):
            if not (index / relative).is_file():
                raise StageFailure(f"External BlockSci directory must contain {relative}: {index}")
        shutil.copytree(index, stage.work / BLOCKSCI_DIR, symlinks=True)
        canonicalize_index(stage)
        found = re.search(r'"maxBlockNum"\s*:\s*(\d+)', (stage.work / BLOCKSCI_CONFIG_FILE).read_text())
        if found is None or int(found.group(1)) <= 0:
            raise StageFailure("External BlockSci config has no positive parser.maxBlockNum")
        max_block = int(found.group(1)) - 1
        stage.log(f"imported external index through block {max_block}")
        return None, max_block, {}
    stage.download(BITCOIN_DATA_DIR)
    stage.download(EXPORTED_BLOCKS_DIR)
    return emulator_bitcoin_datadir(stage), emulator_max_block(stage), {}


def blocksci_parse(stage: Stage) -> None:
    """Parse once and publish a checksummed reusable index, or record it in a persistent directory."""
    settings = cast(BlocksciParseSettings, stage.settings)
    require_tools("zstd")
    stage.download(STAGED_EXPORTERS_DIR)
    source = settings["source"]
    datadir, max_block, extra = parse_source(stage, source)
    index_binds: list[str] = []
    index = None
    if settings["persistent"]:
        # The lock is taken after the block download, which has no index to protect yet.
        index = open_index(stage, settings["persistent"], writer=True)
        if (index.path / "manifest.json").exists() or index.backup_available():
            raise StageFailure(f"[index] an index already exists for {index.path}; use --blocksci-task update")
        # What a failed parse left; a recorded index was refused above.
        index.discard()
        index.begin_write()
        index.link_into_run(stage.work)
        index_binds = index.binds("rw")
    if datadir is not None:
        run_parser(
            stage,
            settings["command"],
            datadir,
            max_block,
            f"parsing {source['description']} through block {max_block}",
            index_binds,
        )
    for relative in INDEX_FILES:
        stage.require_file(relative, f"BlockSci parser did not produce {relative}")
    if source["kind"] == "bitcoin-blocks-s3":
        max_block, block_hash = verify_chain(stage, settings, max_block, update=False, index_binds=index_binds)
        extra["exported_block_hash"] = block_hash
    if index is not None:
        publish_persistent(stage, settings, index, max_block, extra)
    else:
        publish_index(stage, source["kind"], source["network"], max_block, extra)
    stage.log("reusable cache upload complete")


def blocksci_update(stage: Stage) -> None:
    """Extend an index through a higher block from the same block source.

    BlockSci remembers each block by its file number and offset, which only the source
    that was parsed can resolve, so the update reads the same datadir or block archive.
    """
    settings = cast(BlocksciUpdateSettings, stage.settings)
    source = settings["source"]
    kind, network, max_block = source["kind"], source["network"], int(source["max_block"])
    require_tools("zstd")
    stage.download(STAGED_EXPORTERS_DIR)
    source_cache = stage.work / SOURCE_CACHE_DIR
    source_cache.mkdir(parents=True, exist_ok=True)
    index_binds: list[str] = []
    index = None
    if settings["persistent"]:
        index = open_index(stage, settings["persistent"], writer=True)
        index.reconcile()
        manifest = index.manifest()
        check_manifest(manifest, "Persistent BlockSci index", stage.settings["image_name"])
        check_source(manifest, kind, network)
        shutil.copyfile(index.path / "manifest.json", source_cache / "manifest.json")
        source_run_id = str(manifest.get("run_id"))
        index.begin_write()
        index.link_into_run(stage.work)
        shutil.copyfile(index.path / "config.json", stage.work / BLOCKSCI_CONFIG_FILE)
        index_binds = index.binds("rw")
        stage.log(f"[index] updating {index.path} from height {index.height()}")
    else:
        source_run_id = str(settings["source_run_id"])
        stage.log(f"restoring verified cache from run {source_run_id}")
        manifest = restore_index(
            stage,
            source_cache,
            f"{settings['artifact_uri']}/{source_run_id}/{BLOCKSCI_PARSE_DIR}",
            source_run_id,
            "Source cache",
            lambda value: check_source(value, kind, network),
        )
    source_max_block = manifest.get("exported_max_block")
    if not isinstance(source_max_block, int) or isinstance(source_max_block, bool):
        raise StageFailure("Source cache manifest has no exported_max_block")
    if max_block <= source_max_block:
        raise StageFailure(
            f"Target maximum block {max_block} must be greater than source maximum block {source_max_block}"
        )
    extra: dict[str, object] = {}
    if kind == "bitcoin-blocks-s3":
        stage.log(f"downloading block archive files after block {source_max_block}")
        datadir, extra = download_block_archive(stage, source["blocks_uri"], max_block, source_cache / "manifest.json")
    else:
        datadir = Path(str(source["datadir"]))
        if not (datadir / "blocks").is_dir():
            raise StageFailure(f"External Bitcoin coin directory must contain blocks/: {datadir}")
    canonicalize_index(stage)
    set_max_block(stage, max_block)
    run_parser(
        stage,
        settings["command"],
        datadir,
        max_block,
        f"parsing blocks {source_max_block + 1} through {max_block}",
        index_binds,
    )
    if kind == "bitcoin-blocks-s3":
        max_block, block_hash = verify_chain(stage, settings, max_block, update=True, index_binds=index_binds)
        extra["exported_block_hash"] = block_hash
    extra.update(
        cache_operation="incremental-update",
        source_run_id=source_run_id,
        source_exported_max_block=source_max_block,
    )
    if index is not None:
        publish_persistent(stage, settings, index, max_block, extra)
    else:
        publish_index(stage, kind, network, max_block, extra)
    stage.log(f"updated cache upload complete: {source_run_id} -> {stage.run_id}")


def blocksci_backup(stage: Stage) -> None:
    """Replace the S3 backup of the persistent index; analyses keep reading meanwhile."""
    index = open_index(stage, require_persistent(cast(PersistentJobSettings, stage.settings)), writer=False)
    if not index.complete():
        raise StageFailure(f"[index] working copy {index.path} is incomplete; nothing to back up")
    index.backup(threads=stage.settings["ncpus"])
    index.unlock()
    # The lab learns which height the backup holds from the run.
    stage.storage.copy(index.path / "manifest.json", f"{stage.run_prefix}/blocksci-index-backup.json")
    stage.log(f"index at height {index.height()} backed up to {index.backup_uri}")
