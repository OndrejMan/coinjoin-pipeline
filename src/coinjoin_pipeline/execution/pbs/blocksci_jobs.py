"""The S3 PBS jobs over a BlockSci index: parse, update, analyses, notebook and backup.

A run-local index lives in <run>/blocksci_data; the container sees the run under
CONTAINER_RUNS_ROOT. A reusable index is published to <run prefix>/blocksci-parse_data
as an archive, its checksum and, last, its manifest. A persistent index stays in a
shared directory (index_store.py) and only its manifest is copied to the run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import cast

from coinjoin_pipeline.execution.pbs.index_store import IndexStore, usage_report
from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import (
    BlocksciAnalysisSettings,
    BlocksciParseSettings,
    BlocksciSettings,
    BlocksciUpdateSettings,
    OutputSettings,
    PersistentJobSettings,
    PersistentSettings,
    SourceSettings,
)
from coinjoin_pipeline.storage.s5client import S5Error

# The archive of a reusable index in <run prefix>/blocksci-parse_data.
INDEX_ARCHIVE = "blocksci_data.tar.zst"


def emulator_max_block(stage: Stage) -> int:
    """The highest block_<height>.json the emulator exported, which bounds the parser."""
    heights = [
        int(path.stem[len("block_") :])
        for path in (stage.work / "coinjoin_emulator_data/data/btc-node").glob("block_*.json")
        if path.stem[len("block_") :].isdigit()
    ]
    if not heights:
        raise StageFailure("BlockSci parsing could not determine the exported maximum block")
    return max(heights)


def emulator_bitcoin_datadir(stage: Stage) -> Path:
    """The emulator uploads bitcoin_data/ as the datadir itself or with the datadir under data/."""
    datadir = stage.work / "bitcoin_data"
    if not (datadir / "regtest/blocks").is_dir() and (datadir / "data/regtest/blocks").is_dir():
        datadir = datadir / "data"
    if not (datadir / "regtest/blocks").is_dir():
        raise StageFailure(
            "BlockSci requires a Bitcoin datadir containing regtest/blocks; checked "
            f"{stage.work}/bitcoin_data and {stage.work}/bitcoin_data/data"
        )
    return datadir


def publish_index(stage: Stage, source_kind: str, network: str, max_block: int, extra: dict[str, object]) -> None:
    """Stream the run's index to S3; its checksum sidecar and then its manifest make it valid.

    The previous manifest goes first, so a failed upload leaves no index that looks complete.
    """
    prefix = f"{stage.run_prefix}/blocksci-parse_data"
    archive = f"{prefix}/{INDEX_ARCHIVE}"
    stage.log("archiving reusable index")
    stage.storage.remove(f"{prefix}/manifest.json")
    checksum, _ = stage.storage.upload_archive(stage.work, ["blocksci_data"], archive, stage.settings["ncpus"])
    cache = stage.work / "blocksci-parse_data"
    cache.mkdir(parents=True, exist_ok=True)
    sidecar = cache / f"{INDEX_ARCHIVE}.sha256"
    sidecar.write_text(f"{checksum}  {INDEX_ARCHIVE}\n")
    stage.storage.copy(sidecar, f"{archive}.sha256")
    manifest = {
        "schema_version": "1.1",
        "run_id": stage.run_id,
        "blocksci_image": stage.settings["image_name"],
        "source_kind": source_kind,
        "network": network,
        "exported_max_block": max_block,
        **extra,
        "archive": INDEX_ARCHIVE,
        "archive_sha256": checksum,
    }
    (cache / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    stage.storage.copy(cache / "manifest.json", f"{prefix}/manifest.json")


def blocksci(stage: Stage) -> None:
    """Parse the emulated chain, detect CoinJoins and, with the baseline at hand, write the report.

    The parsed index is published like the reusable workflow's, so cached jobs can reuse it.
    """
    settings = cast(BlocksciSettings, stage.settings)
    if shutil.which("zstd") is None:
        raise StageFailure("zstd is required to pack or unpack the BlockSci cache")
    for name in ("bitcoin_data", "coinjoin_emulator_data", ".pipeline/exporters"):
        stage.download(name)
    datadir = emulator_bitcoin_datadir(stage)
    baseline = "coinjoin-analysis_data/coinjoin_tx_info.json"
    if settings["include_report"]:
        stage.download("coinjoin-analysis_data")
        # Retain optional mappings and prior-report provenance without fetching an index.
        for name in ("coinjoin-mappings_data", "coinjoinPipeline_data"):
            stage.download_optional(name, stage.work / name)
        stage.require_file(baseline, f"BlockSci S3-compatible reporting requires {baseline}")
    max_block = emulator_max_block(stage)
    stage.singularity(
        f'cd "{CONTAINER_RUNS_ROOT}/$PBS_RUN_ID" && EXPORTED_MAX_BLOCK="$PBS_EXPORTED_MAX_BLOCK" && '
        + settings["command"],
        binds=(
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{datadir}:/mnt/data:ro",
            f"{stage.work / '.pipeline/exporters'}:/mnt/exporters:ro",
        ),
        environment=(f"PBS_RUN_ID={stage.run_id}", f"PBS_EXPORTED_MAX_BLOCK={max_block}"),
        cleanenv=True,
    )
    outputs = (
        (
            "coinjoinPipeline_data",
            "unified_report.json",
            settings["include_report"],
            "BlockSci S3-compatible reporting",
        ),
        ("blocksci-analysis_data", "blocksci_analysis.json", settings["export_analysis"], "BlockSci analysis"),
    )
    for directory, name, wanted, producer in outputs:
        if wanted:
            stage.require_file(f"{directory}/{name}", f"{producer} did not produce {directory}/{name}")
    publish_index(stage, "emulator", "bitcoin_regtest", max_block, {})
    for directory, _, wanted, _ in reversed(outputs):
        if wanted:
            stage.upload(stage.work / directory, directory)
    stage.storage.run("sync", f"{stage.work / 'logs'}/", f"{stage.settings['log_prefix']}/", check=True, timeout=None)


def container_run(stage: Stage) -> str:
    return f"{CONTAINER_RUNS_ROOT}/{stage.run_id}"


def require_tools(*tools: str) -> None:
    for tool in tools:
        if shutil.which(tool) is None:
            raise StageFailure(f"{tool} is required to pack or unpack the BlockSci cache")


def canonicalize_index(stage: Stage) -> None:
    """An index copied from another location still names its old parsed directory."""
    config = stage.work / "blocksci_data/config.json"
    parsed = f"{container_run(stage)}/blocksci_data/parsed"
    text, replaced = re.subn(
        r'("dataDirectory"\s*:\s*)"[^"]*"', lambda match: f'{match.group(1)}"{parsed}"', config.read_text()
    )
    if not replaced:
        raise StageFailure("Could not canonicalize BlockSci dataDirectory")
    config.write_text(text)


def set_max_block(stage: Stage, max_block: int) -> None:
    """Let the parser read through max_block: BlockSci's maxBlockNum is exclusive."""
    config = stage.work / "blocksci_data/config.json"
    text, replaced = re.subn(
        r'("maxBlockNum"\s*:\s*)-?\d+', lambda match: f"{match.group(1)}{max_block + 1}", config.read_text()
    )
    if not replaced:
        raise StageFailure("Could not update parser.maxBlockNum")
    config.write_text(text)


def check_manifest(manifest: dict, label: str, image_name: str, run_id: str | None = None) -> None:
    """Reject a cache or index manifest of another schema, run or BlockSci image."""
    if manifest.get("schema_version") != "1.1":
        raise StageFailure(f"{label} manifest has an unsupported schema")
    if run_id is not None and manifest.get("run_id") != run_id:
        raise StageFailure(f"{label} run ID does not match {run_id}")
    if manifest.get("blocksci_image") != image_name:
        raise StageFailure(f"{label} was produced by a different BlockSci image")


def check_source(manifest: dict, source_kind: str, network: str) -> None:
    """An update resumes only an index parsed from the same kind of source and network."""
    if manifest.get("source_kind") != source_kind:
        raise StageFailure(f"Incremental update requires a {source_kind} cache")
    if manifest.get("network") != network:
        raise StageFailure(f"Source cache network does not match {network}")


def restore_index(
    stage: Stage, cache_dir: Path, prefix: str, run_id: str, label: str, check: Callable[[dict], None] | None = None
) -> dict:
    """Verify a published index's metadata, then stream it into the run's working index.

    Only small metadata lands in cache_dir. The .writing marker stays on any failure, so
    nothing reads an index whose whole stream was not extracted and verified.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path, sidecar = cache_dir / "manifest.json", cache_dir / f"{INDEX_ARCHIVE}.sha256"
    stage.storage.copy(f"{prefix}/manifest.json", str(manifest_path))
    stage.storage.copy(f"{prefix}/{INDEX_ARCHIVE}.sha256", str(sidecar))
    manifest = json.loads(manifest_path.read_text())
    check_manifest(manifest, label, stage.settings["image_name"], run_id)
    if manifest.get("archive") != INDEX_ARCHIVE:
        raise StageFailure(f"{label} has an unsupported archive")
    if check is not None:
        check(manifest)
    checksum = sidecar.read_text().split(" ", 1)[0].strip()
    if manifest.get("archive_sha256") != checksum:
        raise StageFailure(f"{label} manifest SHA-256 does not match its sidecar")
    (stage.work / "blocksci_data").mkdir(parents=True, exist_ok=True)
    writing = stage.work / "blocksci_data/.writing"
    writing.touch()
    stage.storage.restore_archive(f"{prefix}/{INDEX_ARCHIVE}", stage.work, checksum)
    for relative in ("config.json", "parsed/chain/block.dat"):
        stage.require_file(f"blocksci_data/{relative}", f"{label} did not contain blocksci_data/{relative}")
    canonicalize_index(stage)
    writing.unlink()
    return manifest


def require_persistent(settings: PersistentJobSettings) -> PersistentSettings:
    persistent = settings["persistent"]
    if not persistent:
        raise StageFailure("This job has no persistent index")
    return persistent


def open_index(stage: Stage, settings: PersistentSettings, *, writer: bool) -> IndexStore:
    """Lock the persistent index for reading or writing; finalization releases the lock."""
    require_tools("zstd")
    index = IndexStore(
        str(settings["index_dir"]),
        settings.get("backup_uri"),
        storage=stage.storage,
        lock_seconds=int(settings["lock_seconds"]),
        wait_seconds=float(settings["wait_seconds"]),
        poll_seconds=float(settings["poll_seconds"]),
    )
    index.path.mkdir(parents=True, exist_ok=True)
    stage.finalizers.append(index.unlock)
    if writer:
        index.lock_exclusive()
    else:
        index.lock_shared()
    return index


def scratch_index(stage: Stage, settings: PersistentSettings) -> IndexStore:
    """A private copy of the backup in the job's scratch; nobody else reads it, so no lock."""
    require_tools("zstd")
    index = IndexStore(str(stage.scratch / "index"), settings["backup_uri"], storage=stage.storage)
    index.path.mkdir(parents=True, exist_ok=True)
    index.restore()
    return index


def kept_index(stage: Stage, settings: PersistentSettings) -> IndexStore:
    """A shared copy that stays after the job: filled from the backup when it is incomplete."""
    index = open_index(stage, settings, writer=False)
    if not index.complete():
        index.unlock()
        index.lock_exclusive()
        index.reconcile()
        index.unlock()
        index.lock_shared()
    return index


def report_kept_copy(stage: Stage, directory: Path, uri: str) -> None:
    """Tell the lab how large the kept copy is; a failed report must not stop the notebook."""
    try:
        stage.storage.copy_text(json.dumps(usage_report("notebook", [directory])), uri)
    except (OSError, subprocess.SubprocessError, S5Error) as error:
        stage.log(f"[index] the kept copy's size was not reported: {error}")


def attach_dumplings(stage: Stage, index: IndexStore, uri: str) -> str:
    """The bind of the Dumplings copy beside the index; inside it would break the index's file list.

    Only a finished download takes the name, so an interrupted job leaves no partial copy
    that later jobs would trust. Of two jobs downloading at once, the second drops its own.
    """
    root = index.path.parent / "dumplings"
    scanner = root / "Scanner"
    if not scanner.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".Scanner-", dir=root))
        try:
            stage.storage.copy(f"{uri}/Scanner/*", f"{staging}/", timeout=None)
            try:
                staging.rename(scanner)
            except OSError:
                if not scanner.is_dir():
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return f"{os.path.realpath(scanner)}:/mnt/dumplings/Scanner:ro"


def dumplings_in_place(directory: str) -> str:
    """The bind of a Scanner/ read where it is; a cleared synchronization leaves none."""
    if not Path(directory, "LastProcessedBlockHeight.txt").is_file():
        raise StageFailure(f"[index] {directory} holds no Dumplings Scanner")
    return f"{os.path.realpath(directory)}:/mnt/dumplings/Scanner:ro"


def read_index(stage: Stage, settings: PersistentSettings) -> IndexStore:
    """Read-only access for an analysis: shared lock, complete copy, run-local config."""
    if settings.get("in_scratch"):
        index = scratch_index(stage, settings)
    else:
        index = kept_index(stage, settings) if settings.get("restore") else open_index(stage, settings, writer=False)
    if not index.complete():
        raise StageFailure(
            f"[index] working copy {index.path} is incomplete; a persistent update restores it from the S3 backup"
        )
    check_manifest(index.manifest(), "Persistent BlockSci index", stage.settings["image_name"])
    index.link_into_run(stage.work)
    shutil.copyfile(index.path / "config.json", stage.work / "blocksci_data/config.json")
    canonicalize_index(stage)
    stage.log(f"[index] reading {index.path} at height {index.height()}")
    return index


def publish_persistent(
    stage: Stage,
    settings: BlocksciParseSettings,
    index: IndexStore,
    max_block: int,
    extra: dict[str, object],
) -> None:
    """Record the parsed index in place, publish its height to the run and unlock."""
    stage.log(f"recording persistent index at height {max_block}")
    manifest = {
        "schema_version": "1.1",
        "run_id": stage.run_id,
        "blocksci_image": stage.settings["image_name"],
        "source_kind": settings["source"]["kind"],
        "network": settings["source"]["network"],
        "exported_max_block": max_block,
        **extra,
        "storage": "persistent",
        "index_dir": str(index.path),
        "pbs_resources": settings["pbs_resources"],
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    index.publish(stage.work / "blocksci_data/config.json", manifest)
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
            f"{stage.work / '.pipeline/exporters'}:/mnt/exporters:ro",
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
        f"{run_directory}/blocksci_data/config.json",
        "--max-block",
        str(max_block),
        "--network",
        str(source["network"]),
        *(["--expected-block-hash", str(source["expected_hash"])] if source.get("expected_hash") else []),
        *(["--source-manifest", f"{run_directory}/source-blocksci-parse_data/manifest.json"] if update else []),
        "--output",
        f"{run_directory}/blocksci_data/chain-verification.json",
    ]
    python = shlex.split(settings["python_command"])
    command = stage.image_command(
        "env",
        *python,
        "/mnt/exporters/verify_chain.py",
        *arguments,
        binds=(
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{stage.work / '.pipeline/exporters'}:/mnt/exporters:ro",
            *index_binds,
        ),
    )
    subprocess.run(command, check=True)
    verified = json.loads((stage.work / "blocksci_data/chain-verification.json").read_text())
    return int(verified["exported_max_block"]), str(verified["exported_block_hash"])


def download_block_archive(
    stage: Stage, uri: str, max_block: int, source_manifest: Path | None = None
) -> tuple[Path, dict]:
    """Fetch only the archive files below uri a parse, or an update of source_manifest, needs.

    Returns the block directory's datadir and the cache manifest fields that let a later
    update resume.
    """
    datadir = stage.work / "bitcoin_data"
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
        shutil.copytree(index, stage.work / "blocksci_data", symlinks=True)
        canonicalize_index(stage)
        found = re.search(r'"maxBlockNum"\s*:\s*(\d+)', (stage.work / "blocksci_data/config.json").read_text())
        if found is None or int(found.group(1)) <= 0:
            raise StageFailure("External BlockSci config has no positive parser.maxBlockNum")
        max_block = int(found.group(1)) - 1
        stage.log(f"imported external index through block {max_block}")
        return None, max_block, {}
    stage.download("bitcoin_data")
    stage.download("coinjoin_emulator_data/data/btc-node")
    return emulator_bitcoin_datadir(stage), emulator_max_block(stage), {}


def blocksci_parse(stage: Stage) -> None:
    """Parse once and publish a checksummed reusable index, or record it in a persistent directory."""
    settings = cast(BlocksciParseSettings, stage.settings)
    require_tools("zstd")
    stage.download(".pipeline/exporters")
    source = settings["source"]
    datadir, max_block, extra = parse_source(stage, source)
    index_binds: list[str] = []
    index = None
    if settings["persistent"]:
        # The lock is taken after the block download, which has no index to protect yet.
        index = open_index(stage, settings["persistent"], writer=True)
        if (index.path / "manifest.json").exists() or index.backup_available():
            raise StageFailure(f"[index] an index already exists for {index.path}; use --blocksci-task update")
        for name in ("parsed", "config.json", "files.txt"):
            path = index.path / name
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
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
    for relative in ("config.json", "parsed/chain/block.dat"):
        stage.require_file(f"blocksci_data/{relative}", f"BlockSci parser did not produce blocksci_data/{relative}")
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
    stage.download(".pipeline/exporters")
    source_cache = stage.work / "source-blocksci-parse_data"
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
        shutil.copyfile(index.path / "config.json", stage.work / "blocksci_data/config.json")
        index_binds = index.binds("rw")
        stage.log(f"[index] updating {index.path} from height {index.height()}")
    else:
        source_run_id = str(settings["source_run_id"])
        stage.log(f"restoring verified cache from run {source_run_id}")
        manifest = restore_index(
            stage,
            source_cache,
            f"{settings['artifact_uri']}/{source_run_id}/blocksci-parse_data",
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


def submission_home() -> Path:
    """The submitting user's home: the node may set HOME to another storage volume."""
    return Path(os.environ.get("PBS_O_HOME") or os.environ["HOME"])


def notebook_control(stage: Stage) -> Path:
    """Where the notebook publishes its connection details for the frontend."""
    return submission_home() / ".local/share/coinjoin-pipeline/jupyter" / stage.run_id


def analysis_output(stage: Stage, output: OutputSettings) -> Path:
    """Where the mode writes; a notebook writes to the submitting user's home by default."""
    if output.get("notebooks_dir"):
        return Path(str(output["notebooks_dir"]))
    if output.get("notebook"):
        return submission_home() / "coinjoin-notebooks" / stage.run_id
    return stage.work / str(output["relative"])


def seed_notebook(stage: Stage, output: Path) -> None:
    """The submitting container stages an optional initial file in private S3 metadata."""
    uri = f"{stage.run_prefix}/.notebook/initial.json"
    if not stage.storage.exists(uri):
        return
    seed = stage.storage.cat_json(uri)
    name, text = seed.get("name"), seed.get("text")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,119}\.(py|ipynb)", name)
        or not isinstance(text, str)
        or len(text.encode("utf-8")) > 256 * 1024
        or "\0" in text
    ):
        raise StageFailure("Invalid initial notebook file in S3")
    if name.endswith(".ipynb"):
        document = json.loads(text)
        if (
            not isinstance(document, dict)
            or document.get("nbformat") != 4
            or not isinstance(document.get("cells"), list)
        ):
            raise StageFailure("Invalid version 4 initial notebook")
    # Never overwrite an edited notebook, including when the same run is retried.
    descriptor = os.open(str(output / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


@contextmanager
def notebook_connection(stage: Stage, control: Path) -> Iterator[None]:
    """Publish the current job's private connection while Jupyter is running.

    The frontend container has no shared /storage mount. Its only discovery path is
    this S3 object, checked against qstat before opening a direct node SSH tunnel.
    """
    uri = f"{stage.run_prefix}/.notebook/connection.json"
    stopping = threading.Event()

    def publish() -> None:
        warned = False
        while not stopping.wait(0.2):
            try:
                path = control / "connection.json"
                if not path.is_file():
                    continue
                info = json.loads(path.read_text())
                if info.get("jobId") != os.environ.get("PBS_JOBID") or info.get("runId") != stage.run_id:
                    continue
                stage.storage.copy(path, uri, timeout=15)
                return
            except (OSError, ValueError, subprocess.SubprocessError, S5Error):
                if not warned:
                    stage.log("Notebook connection publication failed; retrying (connection token is not logged)")
                    warned = True
                stopping.wait(1)

    thread = threading.Thread(target=publish, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopping.set()
        thread.join(timeout=16)
        # Stale objects are also rejected by run ID, PBS job ID and terminal qstat state.
        # Wait for an in-flight upload before deleting, so it cannot republish afterwards.
        if not thread.is_alive():
            try:
                if not stage.storage.remove(uri, timeout=15):
                    stage.log("Could not remove ended notebook connection metadata")
            except (OSError, subprocess.SubprocessError, S5Error):
                stage.log("Could not remove ended notebook connection metadata")


def blocksci_analysis(stage: Stage) -> None:
    """Detection, an external comparison, a user script or a notebook over an existing index.

    Outputs are published even when the work fails. The index comes from this run's cache,
    read-only from another run's, or in place from a persistent directory.
    """
    settings = cast(BlocksciAnalysisSettings, stage.settings)
    mode = stage.name
    output = analysis_output(stage, settings["output"])
    stage.finalizers.append(lambda: stage.upload(output, settings["output"]["relative"]))
    require_tools("zstd")
    stage.log(f"[{mode}] downloading required reusable BlockSci inputs")
    for download in settings["inputs"]:
        if "run" in download:
            stage.download(download["run"])
            continue
        target = stage.work / download["to"]
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            stage.storage.copy(download["uri"], str(target))
        except subprocess.CalledProcessError:
            if not download.get("optional"):
                raise
            stage.log(f"[{mode}] the baseline has no {target.name}")
    (stage.work / ".pipeline/exporters").mkdir(parents=True, exist_ok=True)
    binds = [f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw", f"{stage.work / '.pipeline/exporters'}:/mnt/exporters:ro"]
    environment = [
        f"PBS_RUN_ID={stage.run_id}",
        f"ACTIVE_RUN_ID={stage.run_id}",
        f"BLOCKSCI_CONFIG={container_run(stage)}/blocksci_data/config.json",
        f"BLOCKSCI_RUN_DIR={container_run(stage)}",
    ]
    if settings["output"].get("notebooks_dir") and not output.is_dir():
        raise StageFailure(f"BlockSci notebooks directory does not exist on the compute node: {output}")
    output.mkdir(parents=True, exist_ok=True)
    persistent = settings["persistent"]
    if persistent:
        index = read_index(stage, persistent)
        binds += index.binds("ro")
        shutil.copyfile(index.path / "manifest.json", output / "blocksci-index-manifest.json")
        dumplings = persistent.get("dumplings_uri")
        if dumplings:
            binds.append(attach_dumplings(stage, index, dumplings))
        dumplings_dir = persistent.get("dumplings_dir")
        if dumplings_dir:
            binds.append(dumplings_in_place(dumplings_dir))
        report_uri = persistent.get("storage_report_uri")
        if report_uri:
            report_kept_copy(stage, index.path.parent, report_uri)
    else:
        cache_run = settings.get("source_run_id") or stage.run_id
        prefix = f"{settings['artifact_uri']}/{cache_run}/blocksci-parse_data"
        cache = stage.work / "blocksci-parse_data"
        restore_index(stage, cache, prefix, cache_run, "BlockSci index")
        if settings.get("source_run_id"):
            # Record which published index the outputs were computed from.
            shutil.copyfile(cache / "manifest.json", output / "blocksci-cache-manifest.json")
            (output / "blocksci-source.json").write_text(
                json.dumps({"source_run_id": cache_run}, separators=(",", ":")) + "\n"
            )
            binds.append(f"{stage.work / 'blocksci_data/parsed'}:{container_run(stage)}/blocksci_data/parsed:ro")
    if settings.get("user_script"):
        script = Path(str(settings["user_script"]))
        shutil.copyfile(script, output / "script.py")
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        (output / "script.py.sha256").write_text(f"{digest}  {script}\n")
        binds.append(f"{script}:/mnt/user-analysis.py:ro")
    control = None
    if settings["output"].get("notebook"):
        seed_notebook(stage, output)
        control = notebook_control(stage)
        control.mkdir(parents=True, exist_ok=True)
        control.chmod(0o700)
        (control / "connection.json").unlink(missing_ok=True)
        notebook = Path(__file__).parent / "templates/blocksci_notebook.py"
        binds += [
            f"{output}:/mnt/notebooks:rw",
            f"{control}:/mnt/notebook-control:rw",
            f"{notebook}:/mnt/blocksci_notebook.py:ro",
        ]
        environment += [
            f"NOTEBOOK_JOB_ID={os.environ['PBS_JOBID']}",
            f"NOTEBOOK_FRONTEND={os.environ.get('PBS_O_HOST', '')}",
            f"NOTEBOOK_LOGIN={os.environ.get('PBS_O_LOGNAME') or os.environ.get('USER', '')}",
        ]
    stage.log(f"[{mode}] starting on {socket.getfqdn()}")
    for line in settings.get("connection_help", ()):
        stage.log(line)
    with notebook_connection(stage, control) if control is not None else nullcontext():
        stage.singularity(
            f'cd "{CONTAINER_RUNS_ROOT}/$PBS_RUN_ID" && {settings["command"]}',
            binds=binds,
            environment=environment,
            cleanenv=True,
        )
    check = settings.get("output_check")
    if check:
        stage.require_file(check[0], check[1])
    stage.log(f"[{mode}] completed")


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
