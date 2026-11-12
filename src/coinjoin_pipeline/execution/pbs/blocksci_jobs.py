"""The S3 PBS jobs that write a BlockSci index: the combined run, parse and update.

The job that reads an index is ``blocksci_analysis_jobs``; ``blocksci_index`` holds
what both share.
"""

from __future__ import annotations

import json
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
    INDEX_MANIFEST_COPY,
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
    check_manifest,
    config_value,
    container_run,
    edit_config,
    open_index,
    require_tools,
    restore_index,
    run_index,
)
from coinjoin_pipeline.execution.pbs.index_store import index_manifest
from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import (
    BlocksciParseSettings,
    BlocksciSettings,
    BlocksciUpdateSettings,
    IndexWriterSettings,
    PersistentJobSettings,
    PersistentSettings,
    SourceSettings,
)


# An update's copy of the manifest of the index it extends; job scratch, never published.
SOURCE_CACHE_DIR = "source-blocksci-parse_data"
# What verify_chain found; job scratch beside the index, whose directory holds only the index.
CHAIN_VERIFICATION_FILE = "chain-verification.json"


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
    """The index a parse, update or combined job writes, the same way for both stores.

    A persistent index is written in place under its write lock and recorded there; the
    run's own index is written in the job's scratch and published as its single backup
    in <run prefix>/blocksci-parse_data. Both record the same manifest and file list.
    """

    def __init__(self, stage: Stage, persistent: PersistentSettings | None) -> None:
        self.stage = stage
        self.persistent = persistent
        if persistent is None:
            # Fail before parsing: the job ends by packing the run's index.
            require_tools("zstd")
            self.store = run_index(stage, f"{stage.run_prefix}/{BLOCKSCI_PARSE_DIR}")

    @property
    def binds(self) -> list[str]:
        """What the parser binds besides the run: a persistent index lives outside it."""
        return self.store.binds("rw") if self.persistent is not None else []

    def _lock(self) -> None:
        """Take the persistent index's write lock; a run's index lives in this job alone."""
        if self.persistent is not None:
            self.store = open_index(self.stage, self.persistent, writer=True)

    def _link(self) -> None:
        """Mark a persistent index incomplete and point the run's blocksci_data at it."""
        if self.persistent is not None:
            self.store.begin_write()
            self.store.link_into_run(self.stage.work)

    def start_parse(self) -> None:
        """Before a parse from scratch; called after the block download, which has no index to protect."""
        self._lock()
        if self.persistent is not None:
            if (self.store.path / "manifest.json").exists():
                raise StageFailure(f"[index] an index already exists for {self.store.path}; use --blocksci-task update")
            # What a failed parse left; a recorded index was refused above.
            self.store.discard()
        self._link()

    def start_update(self, settings: BlocksciUpdateSettings, source_cache: Path) -> tuple[dict, str]:
        """Bring the index to be extended into the run; its manifest and the run that wrote it.

        source_cache receives that manifest, which the block download compares the archive with.
        """
        kind, network = settings["source"]["kind"], settings["source"]["network"]
        source_cache.mkdir(parents=True, exist_ok=True)
        if self.persistent is None:
            source_run_id = str(settings["source_run_id"])
            self.stage.log(f"restoring verified cache from run {source_run_id}")
            manifest = restore_index(
                self.stage,
                source_cache,
                f"{settings['artifact_uri']}/{source_run_id}/{BLOCKSCI_PARSE_DIR}",
                source_run_id,
                "Source cache",
                lambda value: check_source(value, kind, network),
            )
            return manifest, source_run_id
        self._lock()
        self.store.reconcile()
        manifest = self.store.manifest()
        check_manifest(manifest, "Persistent BlockSci index", self.stage.settings["image_name"])
        check_source(manifest, kind, network)
        shutil.copyfile(self.store.path / "manifest.json", source_cache / "manifest.json")
        self._link()
        shutil.copyfile(self.store.path / "config.json", self.stage.work / BLOCKSCI_CONFIG_FILE)
        self.stage.log(f"[index] updating {self.store.path} from height {self.store.height()}")
        return manifest, str(manifest.get("run_id"))

    def publish(self, source_kind: str, network: str, max_block: int, extra: dict[str, object]) -> None:
        """Record the finished index with the resources of this job, then make it available.

        A run's backup replaces its previous manifest first, so a failed upload leaves no index
        that looks complete. The lab cannot see a persistent index's multijob scratch, so it
        reads that index's height from a copy of the manifest in the run prefix.
        """
        stage = self.stage
        extra = {**extra, "pbs_resources": cast(IndexWriterSettings, stage.settings)["pbs_resources"]}
        manifest = index_manifest(stage.run_id, stage.settings["image_name"], source_kind, network, max_block, extra)
        config = stage.work / BLOCKSCI_CONFIG_FILE
        if self.persistent is None:
            stage.log("archiving reusable index")
            self.store.publish(config, manifest, in_place=False)
            self.store.backup(threads=stage.settings["ncpus"])
            stage.log("reusable cache upload complete")
            return
        stage.log(f"recording persistent index at height {max_block}")
        self.store.publish(config, manifest)
        stage.storage.copy(self.store.path / "manifest.json", f"{stage.run_prefix}/{INDEX_MANIFEST_COPY}")
        self.store.unlock()


def blocksci(stage: Stage) -> None:
    """Parse the emulated chain, detect CoinJoins and, with the baseline at hand, write the report.

    The parsed index is published like the reusable workflow's, so cached jobs can reuse it.
    """
    settings = cast(BlocksciSettings, stage.settings)
    index = WrittenIndex(stage, None)
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


def set_max_block(stage: Stage, max_block: int) -> None:
    """Let the parser read through max_block: BlockSci's maxBlockNum is exclusive."""
    edit_config(stage, "parser", "maxBlockNum", max_block + 1)


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


def run_parser(
    stage: Stage, command: str, datadir: Path, max_block: int, message: str, index_binds: Sequence[str] = ()
) -> None:
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
            *index_binds,
        ),
        environment=(f"PBS_RUN_ID={stage.run_id}", f"PBS_EXPORTED_MAX_BLOCK={max_block}"),
        cleanenv=True,
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
        f"{run_directory}/{CHAIN_VERIFICATION_FILE}",
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
        cleanenv=True,
    )
    subprocess.run(command, check=True)
    verified = json.loads((stage.work / CHAIN_VERIFICATION_FILE).read_text())
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
    return datadir, json.loads(fields.read_text())


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
        return None, max_block, {}
    stage.download(BITCOIN_DATA_DIR)
    stage.download(EXPORTED_BLOCKS_DIR)
    return emulator_bitcoin_datadir(stage), emulator_max_block(stage), {}


def blocksci_parse(stage: Stage) -> None:
    """Parse once and publish a checksummed reusable index, or record it in a persistent directory."""
    settings = cast(BlocksciParseSettings, stage.settings)
    index = WrittenIndex(stage, settings["persistent"])
    stage.download(STAGED_EXPORTERS_DIR)
    source = settings["source"]
    datadir, max_block, extra = parse_source(stage, source)
    index.start_parse()
    if datadir is not None:
        run_parser(
            stage,
            settings["command"],
            datadir,
            max_block,
            f"parsing {source['description']} through block {max_block}",
            index.binds,
        )
    for relative in INDEX_FILES:
        stage.require_file(relative, f"BlockSci parser did not produce {relative}")
    if source["kind"] == "bitcoin-blocks-s3":
        max_block, block_hash = verify_chain(stage, settings, max_block, update=False, index_binds=index.binds)
        extra["exported_block_hash"] = block_hash
    index.publish(source["kind"], source["network"], max_block, extra)


def blocksci_update(stage: Stage) -> None:
    """Extend an index through a higher block from the same block source.

    BlockSci remembers each block by its file number and offset, which only the source
    that was parsed can resolve, so the update reads the same datadir or block archive.
    """
    settings = cast(BlocksciUpdateSettings, stage.settings)
    source = settings["source"]
    kind, network, max_block = source["kind"], source["network"], int(source["max_block"])
    index = WrittenIndex(stage, settings["persistent"])
    stage.download(STAGED_EXPORTERS_DIR)
    source_cache = stage.work / SOURCE_CACHE_DIR
    manifest, source_run_id = index.start_update(settings, source_cache)
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
        index.binds,
    )
    if kind == "bitcoin-blocks-s3":
        max_block, block_hash = verify_chain(stage, settings, max_block, update=True, index_binds=index.binds)
        extra["exported_block_hash"] = block_hash
    extra.update(
        cache_operation="incremental-update",
        source_run_id=source_run_id,
        source_exported_max_block=source_max_block,
    )
    index.publish(kind, network, max_block, extra)
    stage.log(f"updated index published: {source_run_id} -> {stage.run_id}")
