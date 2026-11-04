"""Self-contained worker of the S3 PBS stages that run as Python instead of shell.

A converted job ships this file, ``s5client.py`` and its ``settings.json`` to the
compute node and runs ``python3 stage_runtime.py settings.json``. Standard library
only and Python 3.8 syntax.

Whatever happens, the stage writes and uploads ``.pbs/<stage>.done`` or
``.pbs/<stage>.failed`` (the frontend waits on them) after its outputs, then empties
the job's scratch directory. The exit status is the work's; a failed output or marker
upload turns success into that upload's status.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, TypedDict

try:
    from coinjoin_pipeline.execution.pbs.index_store import IndexStore
    from coinjoin_pipeline.storage.s5client import S5Client
except ImportError:  # the PBS job ships index_store.py and s5client.py next to this script
    from index_store import IndexStore  # type: ignore[no-redef]
    from s5client import S5Client  # type: ignore[no-redef]

# Where the containers see the runs: exporters read runs below the first root, the
# coinjoin-analysis collector scans every run below the second.
CONTAINER_RUNS_ROOT = "/runs/emulation/logs"
CONTAINER_SELECTED_ROOT = "/runs/emulation/selected"


class StageSettings(TypedDict, total=False):
    """settings.json, written by templates_s3.render_python_stage for one job."""

    stage: str
    # The worker function; several jobs share a stage name and its markers.
    job: str
    run_id: str
    run_prefix: str
    endpoint: str
    credentials: str
    profile: str
    image: str
    # The configured image name; index manifests record it so a rebuilt tag still matches.
    image_name: str
    log_prefix: str
    ncpus: int
    # The shell command run in the image.
    command: str
    # unified-report
    downloads: list[str]
    # coinjoin-tools: one invocation of a shipped analyzer CLI
    program: str
    output_directory: str
    input_uri: str | None
    run_input: bool
    invocation: dict[str, object]
    files: dict[str, str]
    # A log the job's whole output goes to, uploaded beside the run's logs (blocksci-parse).
    job_log: str
    # coinjoin-mappings: the shell shared with the shared-storage job, and its images
    script: str
    enumerator_image: str
    sake_image: str
    # blocksci: the combined parse and analysis of an emulation
    include_report: bool
    export_analysis: bool
    # blocksci-parse, blocksci-update and the analyses over an existing index
    artifact_uri: str
    python_command: str
    pbs_resources: dict
    source: dict
    source_run_id: str | None
    persistent: dict | None
    output: dict
    inputs: list[dict]
    user_script: str | None
    connection_help: list[str]
    output_check: list[str] | None
    # dumplings-baseline
    dumplings_uri: str
    mix_id: str
    false_cjtxs: str
    # Run-relative path of the manifest copy the external comparison reads.
    snapshot_file: str


def pipe(stream: IO[bytes] | None) -> IO[bytes]:
    if stream is None:
        raise RuntimeError("The process has no pipe to stream through")
    return stream


def failure_status(error: BaseException) -> int:
    """A failed command's own exit status, else 1."""
    if isinstance(error, subprocess.CalledProcessError) and error.returncode > 0:
        return error.returncode
    return 1


class StageFailure(Exception):
    """A precondition or output check failed; the message is the job's diagnosis."""


class Stage:
    """One job's run tree on scratch, its S3 run prefix and its terminal markers."""

    def __init__(self, settings: StageSettings, scratch: Path) -> None:
        self.settings = settings
        self.name = settings["stage"]
        self.run_id = settings["run_id"]
        self.run_prefix = settings["run_prefix"]
        self.scratch = scratch
        self.runs_root = scratch / "coinjoin-run"
        self.work = self.runs_root / self.run_id
        self.storage = S5Client(settings["endpoint"], settings["credentials"], settings["profile"])
        # Uploads that run after the job body whatever its outcome, before the markers.
        self.finalizers: list[Callable[[], None]] = []

    def log(self, message: str) -> None:
        print(f"[{self.name}] {message}", flush=True)

    def download(self, name: str, destination: Path | None = None) -> None:
        """Sync one run directory from the run prefix, by default into the same place of the run tree."""
        target = self.work / name if destination is None else destination
        target.mkdir(parents=True, exist_ok=True)
        self.storage.run("sync", f"{self.run_prefix}/{name}/*", f"{target}/", check=True, timeout=None)

    def download_optional(self, name: str, destination: Path) -> None:
        """Like download, but a run directory no earlier stage produced is skipped."""
        if self.storage.listing(f"{self.run_prefix}/{name}/*"):
            self.download(name, destination)

    def upload(self, source: Path, name: str) -> None:
        self.storage.run("sync", f"{source}/", f"{self.run_prefix}/{name}/", check=True, timeout=None)

    def require_file(self, relative: str, message: str) -> None:
        if not (self.work / relative).is_file():
            raise StageFailure(message)

    def singularity(
        self,
        script: str,
        *,
        binds: Sequence[str] = (),
        environment: Sequence[str] = (),
        cleanenv: bool = False,
        log: Path | None = None,
    ) -> None:
        """Run a shell script in the stage image; with log, its output is also kept in that file."""
        arguments = ["singularity", "exec", *(["--cleanenv"] if cleanenv else [])]
        for bind in binds:
            arguments += ["--bind", bind]
        for variable in environment:
            arguments += ["--env", variable]
        arguments += [self.settings["image"], "bash", "-c", script]
        if log is None:
            subprocess.run(arguments, check=True)
            return
        with (
            subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process,
            log.open("wb") as kept,
        ):
            for line in iter(pipe(process.stdout).readline, b""):
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
                kept.write(line)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, arguments)

    def container(self, root: str, binds: Sequence[str]) -> None:
        """Run the stage command from the run directory below root as the container sees it."""
        self.singularity(
            f'cd "{root}/$PBS_RUN_ID" && {self.settings["command"]}',
            binds=binds,
            environment=(f"PBS_RUN_ID={self.run_id}",),
        )

    def finalize(self, status: int) -> int:
        """Run every finalizer; a failing one fails a job that had succeeded."""
        for finalizer in self.finalizers:
            try:
                finalizer()
            except Exception as error:
                print(f"[{self.name}] finalization failed: {error}", file=sys.stderr)
                status = status or failure_status(error)
        return status

    def publish_marker(self, outcome: str) -> None:
        marker = self.work / ".pbs" / f"{self.name}.{outcome}"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(outcome + "\n")
        self.storage.copy(marker, f"{self.run_prefix}/.pbs/{self.name}.{outcome}")

    def finish(self, status: int) -> int:
        """Publish the terminal marker; a done marker that fails to upload turns into a failure."""
        if status == 0:
            try:
                self.publish_marker("done")
            except Exception as error:
                print(f"[{self.name}] done marker upload failed: {error}", file=sys.stderr)
                status = failure_status(error)
        if status != 0:
            try:
                self.publish_marker("failed")
            except Exception as error:
                print(f"[{self.name}] failed marker upload failed: {error}", file=sys.stderr)
        return status


def unified_report(stage: Stage) -> None:
    """Join the precomputed analyzer outputs into the JSON and Markdown report."""
    settings = stage.settings
    stage.log("downloading lightweight report inputs")
    for name in settings["downloads"]:
        stage.download(name)
    for relative in (
        "blocksci-analysis_data/blocksci_analysis.json",
        "coinjoin-analysis_data/coinjoin_tx_info.json",
        ".pipeline/exporters/unified_report.py",
    ):
        stage.require_file(relative, f"Unified S3 report requires {relative}")
    stage.log("assembling JSON and Markdown from precomputed analyzer outputs")
    stage.container(
        CONTAINER_RUNS_ROOT,
        (
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{stage.work / '.pipeline/exporters'}:/mnt/exporters:ro",
        ),
    )
    stage.require_file(
        "coinjoinPipeline_data/unified_report.json",
        "Unified S3 report did not produce coinjoinPipeline_data/unified_report.json",
    )
    stage.upload(stage.work / "coinjoinPipeline_data", "coinjoinPipeline_data")
    stage.log("report upload complete")


def coinjoin_analysis(stage: Stage) -> None:
    """Run the coinjoin-analysis baseline over the emulator's wallet and coordinator logs.

    The collector scans every run below its root, so the container sees a root that
    holds this run alone: the output directory, with the emulator data read-only in it.
    """
    stage.download("coinjoin_emulator_data")
    data = stage.work / "coinjoin_emulator_data/data"
    if not data.is_dir():
        raise StageFailure("Coinjoin analysis requires coinjoin_emulator_data/data")
    output = stage.work / "coinjoin-analysis_data"
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
    )
    stage.require_file(
        "coinjoin-analysis_data/coinjoin_tx_info.json",
        "Coinjoin analysis did not produce coinjoin-analysis_data/coinjoin_tx_info.json",
    )
    stage.upload(output, "coinjoin-analysis_data")


def coinjoin_tools(stage: Stage) -> None:
    """One invocation of a shipped coinjoin-analysis CLI on a copy of its input.

    Every file it leaves, its log and invocation included, is published under its own
    result directory even when the analyzer fails; the comparison baseline is untouched.
    """
    settings = stage.settings
    result_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.environ.get('PBS_JOBID') or 'manual'}"
    relative = f"{settings['output_directory']}/{settings['program']}/{result_id}"
    output = stage.work / relative
    (output / "input").mkdir(parents=True)
    stage.finalizers.append(lambda: stage.upload(output, relative))
    if settings.get("input_uri"):
        source = str(settings["input_uri"]).rstrip("/")
        stage.storage.run("sync", f"{source}/*", f"{output / 'input'}/", check=True, timeout=None)
    elif settings["run_input"]:
        experiment = output / "input" / stage.run_id
        experiment.mkdir(parents=True)
        stage.download_optional("coinjoin-analysis_data", experiment)
        stage.download_optional("coinjoin_emulator_data/data", experiment / "data")
        if not any(path.is_file() for path in experiment.rglob("*")):
            raise StageFailure("The run has no baseline or emulator data; provide analysis_input_uri")
    (output / "invocation.json").write_text(json.dumps(settings["invocation"], indent=2) + "\n")
    for name, content in settings["files"].items():
        (output / name).write_text(content)
    stage.singularity(
        settings["command"],
        binds=(f"{output}:/work:rw",),
        environment=("MPLBACKEND=Agg", "MPLCONFIGDIR=/tmp/matplotlib"),
        cleanenv=True,
        log=output / "analysis.log",
    )


def dumplings_baseline(stage: Stage) -> None:
    """The mainnet baseline: coinjoin-analysis over the Dumplings copy mainnet-sync publishes.

    ``manifest.json``, written last, names the copy's height and file sizes; a copy
    without it is being replaced or its upload failed.
    """
    settings = stage.settings
    uri, mix_id = settings["dumplings_uri"], settings["mix_id"]
    output = stage.work / "coinjoin-analysis_data"
    output.mkdir(parents=True, exist_ok=True)
    stage.log(f"reading the Dumplings copy from {uri}")
    manifest_path = stage.work / "dumplings-manifest.json"
    stage.storage.copy(f"{uri}/manifest.json", str(manifest_path))
    manifest = json.loads(manifest_path.read_text())
    height = manifest["last_processed_height"]
    stage.log(f"Dumplings Scanner/ at height {height}")
    # Parent of Scanner/: parse_dumplings also writes summary.log and operation.txt one level up.
    work = stage.scratch / "dumplings-work"
    scanner = work / "dumplings/Scanner"
    scanner.mkdir(parents=True)
    stage.storage.copy(f"{uri}/Scanner/*", f"{scanner}/", timeout=None)
    sizes = {
        os.path.relpath(os.path.join(directory, name), scanner): os.path.getsize(os.path.join(directory, name))
        for directory, _, names in os.walk(scanner)
        for name in names
    }
    if sizes != manifest["files"]:
        raise StageFailure("the downloaded Scanner/ does not match its manifest")
    # coinjoin-analysis reads <file>.json instead of a Dumplings file when one exists and
    # never checks its age, so the snapshot must arrive without them.
    for cached in scanner.glob("*.txt.json"):
        cached.unlink()
    stage.singularity(
        f"cd /work && {settings['command']}",
        binds=(f"{work}:/work:rw",),
        environment=("MPLCONFIGDIR=/tmp",),
        cleanenv=True,
    )
    baseline = scanner / mix_id / "coinjoin_tx_info.json"
    if not baseline.is_file():
        raise StageFailure(f"coinjoin-analysis did not produce Scanner/{mix_id}/coinjoin_tx_info.json")
    shutil.copyfile(baseline, output / "coinjoin_tx_info.json")
    # The curated false positives shipped with this coinjoin-analysis image, for the comparison.
    false_cjtxs = output / "false_cjtxs.json"
    curated = f"/opt/coinjoin-analysis/data/{settings['false_cjtxs']}/false_cjtxs.json"
    with false_cjtxs.open("wb") as handle:
        copied = subprocess.run(["singularity", "exec", "--cleanenv", settings["image"], "cat", curated], stdout=handle)
    if copied.returncode:
        print(f"[{stage.name}] the image has no curated false_cjtxs.json", file=sys.stderr)
        false_cjtxs.unlink()
    shutil.copyfile(manifest_path, stage.work / settings["snapshot_file"])
    coinjoins = len(json.loads((output / "coinjoin_tx_info.json").read_text())["coinjoins"])
    stage.log(f"baseline of {coinjoins} coinjoins through height {height}")
    stage.upload(output, "coinjoin-analysis_data")


def coinjoin_mappings(stage: Stage) -> None:
    """Wasabi input-output mappings and Sake over the coinjoin-analysis baseline.

    The enumerator and Sake run from ``templates/mappings_run.sh``, which the
    shared-storage job runs too; their output is published even when they fail.
    """
    settings = stage.settings
    output = stage.work / "coinjoin-mappings_data"
    output.mkdir(parents=True, exist_ok=True)
    stage.finalizers.append(lambda: stage.upload(output, "coinjoin-mappings_data"))
    stage.log("downloading coinjoin-analysis input")
    stage.download("coinjoin-analysis_data")
    baseline = "coinjoin-analysis_data/coinjoin_tx_info.json"
    stage.require_file(baseline, f"CoinJoin mappings require {baseline}")
    environment = {
        **os.environ,
        "WORK": str(stage.work),
        "OUT": str(output),
        "INPUT": str(stage.work / baseline),
        "ENUMERATOR_IMAGE": settings["enumerator_image"],
        "SAKE_IMAGE": settings["sake_image"],
    }
    subprocess.run(["bash", "-euo", "pipefail", "-c", settings["script"]], env=environment, check=True)
    stage.log("completed")


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
    archive = f"{prefix}/blocksci_data.tar.zst"
    stage.log("archiving reusable index")
    stage.storage.remove(f"{prefix}/manifest.json")
    checksum, _ = stage.storage.upload_archive(stage.work, ["blocksci_data"], archive, stage.settings["ncpus"])
    cache = stage.work / "blocksci-parse_data"
    cache.mkdir(parents=True, exist_ok=True)
    sidecar = cache / "blocksci_data.tar.zst.sha256"
    sidecar.write_text(f"{checksum}  blocksci_data.tar.zst\n")
    stage.storage.copy(sidecar, f"{archive}.sha256")
    manifest = {
        "schema_version": "1.1",
        "run_id": stage.run_id,
        "blocksci_image": stage.settings["image_name"],
        "source_kind": source_kind,
        "network": network,
        "exported_max_block": max_block,
        **extra,
        "archive": "blocksci_data.tar.zst",
        "archive_sha256": checksum,
    }
    (cache / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    stage.storage.copy(cache / "manifest.json", f"{prefix}/manifest.json")


def blocksci(stage: Stage) -> None:
    """Parse the emulated chain, detect CoinJoins and, with the baseline at hand, write the report.

    The parsed index is published like the reusable workflow's, so cached jobs can reuse it.
    """
    settings = stage.settings
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
    stage.storage.run("sync", f"{stage.work / 'logs'}/", f"{settings['log_prefix']}/", check=True, timeout=None)


# BlockSci ---------------------------------------------------------------------
#
# A run-local index lives in <run>/blocksci_data; the container sees the run under
# CONTAINER_RUNS_ROOT. A reusable index is published to <run prefix>/blocksci-parse_data
# as an archive, its checksum and, last, its manifest. A persistent index stays in a
# shared directory (index_store.py) and only its manifest is copied to the run.

INDEX_ARCHIVE = "blocksci_data.tar.zst"


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


def open_index(stage: Stage, *, writer: bool) -> IndexStore:
    """Lock the persistent index for reading or writing; finalization releases the lock."""
    require_tools("zstd")
    settings = stage.settings.get("persistent")
    if not settings:
        raise StageFailure("This job has no persistent index")
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


def read_index(stage: Stage) -> IndexStore:
    """Read-only access for an analysis: shared lock, complete copy, run-local config."""
    index = open_index(stage, writer=False)
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
    stage: Stage, index: IndexStore, source_kind: str, network: str, max_block: int, extra: dict[str, object]
) -> None:
    """Record the parsed index in place, publish its height to the run and unlock."""
    stage.log(f"recording persistent index at height {max_block}")
    manifest = {
        "schema_version": "1.1",
        "run_id": stage.run_id,
        "blocksci_image": stage.settings["image_name"],
        "source_kind": source_kind,
        "network": network,
        "exported_max_block": max_block,
        **extra,
        "storage": "persistent",
        "index_dir": str(index.path),
        "pbs_resources": stage.settings["pbs_resources"],
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    index.publish(stage.work / "blocksci_data/config.json", manifest)
    # The lab cannot see the multijob scratch; it reads the index height from here.
    stage.storage.copy(index.path / "manifest.json", f"{stage.run_prefix}/blocksci-index-manifest.json")
    index.unlock()


def run_parser(stage: Stage, datadir: Path, max_block: int, message: str, index_binds: Sequence[str] = ()) -> None:
    """Run the stage's parse command over datadir inside the BlockSci image."""
    stage.log(message)
    stage.singularity(
        f'cd "{CONTAINER_RUNS_ROOT}/$PBS_RUN_ID" && {stage.settings["command"]}',
        binds=(
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{stage.work / '.pipeline/exporters'}:/mnt/exporters:ro",
            f"{datadir}:/mnt/data:ro",
            *index_binds,
        ),
        environment=(f"PBS_RUN_ID={stage.run_id}", f"PBS_EXPORTED_MAX_BLOCK={max_block}"),
    )


def verify_chain(stage: Stage, max_block: int, *, update: bool, index_binds: Sequence[str]) -> tuple[int, str]:
    """Check the parsed chain against the block archive before anything publishes it."""
    source = stage.settings["source"]
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
    command = ["singularity", "exec"]
    for bind in (
        f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
        f"{stage.work / '.pipeline/exporters'}:/mnt/exporters:ro",
        *index_binds,
    ):
        command += ["--bind", bind]
    python = shlex.split(stage.settings["python_command"])
    command += [stage.settings["image"], "env", *python, "/mnt/exporters/verify_chain.py", *arguments]
    subprocess.run(command, check=True)
    verified = json.loads((stage.work / "blocksci_data/chain-verification.json").read_text())
    return int(verified["exported_max_block"]), str(verified["exported_block_hash"])


def download_block_archive(stage: Stage, max_block: int, source_manifest: Path | None = None) -> tuple[Path, dict]:
    """Fetch only the archive files a parse, or an update of source_manifest, needs.

    Returns the block directory's datadir and the cache manifest fields that let a later
    update resume.
    """
    uri = stage.settings["source"]["blocks_uri"]
    datadir = stage.work / "bitcoin_data"
    work = stage.work / "block-archive"
    for directory in (datadir / "blocks", work / "sidecars"):
        directory.mkdir(parents=True, exist_ok=True)
    stage.storage.copy(f"{uri}/blk*.dat.json", f"{work / 'sidecars'}/", timeout=None)
    # Shipped beside this worker; in the source tree it sits one directory up.
    shipped = Path(__file__).with_name("block_archive.py")
    helper = [sys.executable, str(shipped if shipped.is_file() else Path(__file__).parents[1] / "block_archive.py")]
    update = ["--source-manifest", str(source_manifest)] if source_manifest is not None else []
    commands, fields = work / "download.s5cmd", work / "manifest-fields.json"
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
            "--commands",
            str(commands),
            "--manifest-fields",
            str(fields),
        ],
        check=True,
    )
    stage.storage.run("run", str(commands), check=True, timeout=None)
    subprocess.run([*helper, "verify", str(datadir / "blocks"), "--max-block", str(max_block), *update], check=True)
    # The helper writes the fields as they follow another manifest field.
    return datadir, json.loads("{" + fields.read_text().strip().lstrip(",") + "}")


def parse_source(stage: Stage) -> tuple[Path | None, int, dict]:
    """Prepare the parse's single source: its datadir (None for a copied index), height and manifest fields."""
    source = stage.settings["source"]
    kind = source["kind"]
    stage.log(f"preparing {source['description']}")
    if kind == "external-bitcoin":
        datadir = Path(str(source["datadir"]))
        if not (datadir / "blocks").is_dir():
            raise StageFailure(f"External Bitcoin coin directory must contain blocks/: {datadir}")
        return datadir, int(source["max_block"]), {}
    if kind == "bitcoin-blocks-s3":
        datadir, extra = download_block_archive(stage, int(source["max_block"]))
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


def with_job_log(stage: Stage, name: str) -> None:
    """Send the job's output to a log the finalization uploads beside the run's other logs."""
    log = stage.work / "logs" / name
    log.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(descriptor, 1)
    os.dup2(descriptor, 2)
    os.close(descriptor)

    def restore_and_upload() -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        stage.storage.copy(log, f"{stage.settings['log_prefix']}/{name}")

    # Before every later finalizer, the index lock release included.
    stage.finalizers.insert(0, restore_and_upload)


def blocksci_parse(stage: Stage) -> None:
    """Parse once and publish a checksummed reusable index, or record it in a persistent directory."""
    settings = stage.settings
    require_tools("zstd")
    stage.download(".pipeline/exporters")
    source = settings["source"]
    datadir, max_block, extra = parse_source(stage)
    index_binds: list[str] = []
    index = None
    if settings.get("persistent"):
        # The lock is taken after the block download, which has no index to protect yet.
        index = open_index(stage, writer=True)
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
        run_parser(stage, datadir, max_block, f"parsing {source['description']} through block {max_block}", index_binds)
    for relative in ("config.json", "parsed/chain/block.dat"):
        stage.require_file(f"blocksci_data/{relative}", f"BlockSci parser did not produce blocksci_data/{relative}")
    if source["kind"] == "bitcoin-blocks-s3":
        max_block, block_hash = verify_chain(stage, max_block, update=False, index_binds=index_binds)
        extra["exported_block_hash"] = block_hash
    if index is not None:
        publish_persistent(stage, index, source["kind"], source["network"], max_block, extra)
    else:
        publish_index(stage, source["kind"], source["network"], max_block, extra)
    stage.log("reusable cache upload complete")


def blocksci_update(stage: Stage) -> None:
    """Extend an index through a higher block from the same block source.

    BlockSci remembers each block by its file number and offset, which only the source
    that was parsed can resolve, so the update reads the same datadir or block archive.
    """
    settings = stage.settings
    source = settings["source"]
    kind, network, max_block = source["kind"], source["network"], int(source["max_block"])
    require_tools("zstd")
    stage.download(".pipeline/exporters")
    source_cache = stage.work / "source-blocksci-parse_data"
    source_cache.mkdir(parents=True, exist_ok=True)
    index_binds: list[str] = []
    index = None
    if settings.get("persistent"):
        index = open_index(stage, writer=True)
        index.reconcile()
        manifest = index.manifest()
        check_manifest(manifest, "Persistent BlockSci index", settings["image_name"])
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
        datadir, extra = download_block_archive(stage, max_block, source_cache / "manifest.json")
    else:
        datadir = Path(str(source["datadir"]))
        if not (datadir / "blocks").is_dir():
            raise StageFailure(f"External Bitcoin coin directory must contain blocks/: {datadir}")
    canonicalize_index(stage)
    set_max_block(stage, max_block)
    run_parser(stage, datadir, max_block, f"parsing blocks {source_max_block + 1} through {max_block}", index_binds)
    if kind == "bitcoin-blocks-s3":
        max_block, block_hash = verify_chain(stage, max_block, update=True, index_binds=index_binds)
        extra["exported_block_hash"] = block_hash
    extra.update(
        cache_operation="incremental-update",
        source_run_id=source_run_id,
        source_exported_max_block=source_max_block,
    )
    if index is not None:
        publish_persistent(stage, index, kind, network, max_block, extra)
    else:
        publish_index(stage, kind, network, max_block, extra)
    stage.log(f"updated cache upload complete: {source_run_id} -> {stage.run_id}")


def submission_home() -> Path:
    """The submitting user's home: the node may set HOME to another storage volume."""
    return Path(os.environ.get("PBS_O_HOME") or os.environ["HOME"])


def notebook_control(stage: Stage) -> Path:
    """Where the notebook publishes its connection details for the frontend."""
    return submission_home() / ".local/share/coinjoin-pipeline/jupyter" / stage.run_id


def analysis_output(stage: Stage) -> Path:
    """Where the mode writes; a notebook writes to the submitting user's home by default."""
    output = stage.settings["output"]
    if output.get("notebooks_dir"):
        return Path(str(output["notebooks_dir"]))
    if output.get("notebook"):
        return submission_home() / "coinjoin-notebooks" / stage.run_id
    return stage.work / str(output["relative"])


def blocksci_analysis(stage: Stage) -> None:
    """Detection, an external comparison, a user script or a notebook over an existing index.

    Outputs are published even when the work fails. The index comes from this run's cache,
    read-only from another run's, or in place from a persistent directory.
    """
    settings = stage.settings
    mode = stage.name
    output = analysis_output(stage)
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
    output.mkdir(parents=True, exist_ok=True)
    if settings.get("persistent"):
        index = read_index(stage)
        binds += index.binds("ro")
        shutil.copyfile(index.path / "manifest.json", output / "blocksci-index-manifest.json")
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
    if settings["output"].get("notebook"):
        control = notebook_control(stage)
        control.mkdir(parents=True, exist_ok=True)
        control.chmod(0o700)
        (control / "connection.json").unlink(missing_ok=True)
        notebook = Path(__file__).with_name("blocksci_notebook.py")
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
    index = open_index(stage, writer=False)
    if not index.complete():
        raise StageFailure(f"[index] working copy {index.path} is incomplete; nothing to back up")
    index.backup(threads=stage.settings["ncpus"])
    index.unlock()
    # The lab learns which height the backup holds from the run.
    stage.storage.copy(index.path / "manifest.json", f"{stage.run_prefix}/blocksci-index-backup.json")
    stage.log(f"index at height {index.height()} backed up to {index.backup_uri}")


JOBS: dict[str, Callable[[Stage], None]] = {
    "blocksci-parse": blocksci_parse,
    "blocksci-update": blocksci_update,
    "blocksci-analysis": blocksci_analysis,
    "blocksci-backup": blocksci_backup,
    "blocksci": blocksci,
    "coinjoin-mappings": coinjoin_mappings,
    "unified-report": unified_report,
    "coinjoin-analysis": coinjoin_analysis,
    "coinjoin-tools": coinjoin_tools,
    "dumplings-baseline": dumplings_baseline,
}


def clean_scratch(scratch: Path) -> None:
    """Empty the scratch once the markers are in S3; MetaCentrum would keep it for 14 days.

    Only the contents go: the system removes the directory itself.
    """
    if shutil.which("clean_scratch"):
        subprocess.run(["clean_scratch"], check=False)
        return
    if not scratch.is_dir():
        return
    for entry in scratch.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink()


def run(stage: Stage) -> int:
    """The job body; the exit status a shell job with ``set -e`` would end with."""
    try:
        if stage.settings.get("job_log"):
            # Before any check, so even a missing credential reaches the uploaded log.
            with_job_log(stage, str(stage.settings["job_log"]))
        if not os.access(stage.settings["credentials"], os.R_OK):
            raise StageFailure(f"S3 credentials file is not readable: {stage.settings['credentials']}")
        if shutil.which("s5cmd") is None:
            raise StageFailure("s5cmd is required")
        for directory in (stage.work / ".pbs", stage.work / "logs"):
            directory.mkdir(parents=True, exist_ok=True)
        JOBS[stage.settings["job"]](stage)
    except subprocess.CalledProcessError as error:
        print(f"[{stage.name}] {error}", file=sys.stderr)
        return error.returncode or 1
    except Exception as error:
        # Any failure, a lock or transfer error included, must still publish the failed marker.
        print(f"[{stage.name}] {error}", file=sys.stderr)
        return 1
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    scratch = os.environ.get("SCRATCHDIR")
    if not scratch:
        print("SCRATCHDIR is not set", file=sys.stderr)
        return 1
    settings: StageSettings = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    for name in ("TMPDIR", "SINGULARITY_CACHEDIR", "SINGULARITY_TMPDIR", "SINGULARITY_LOCALCACHEDIR"):
        os.environ[name] = scratch
    stage = Stage(settings, Path(scratch))

    def terminated(_signum: int, _frame: object) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminated)
    status = stage.finish(stage.finalize(run(stage)))
    clean_scratch(stage.scratch)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
