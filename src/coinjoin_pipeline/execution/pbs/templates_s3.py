"""S3-backed PBS job renderers.

Every job is ``directives`` and a few lines of shell that write the stage worker's
modules (``scripts.STAGE_RUNTIME_MODULES``) and a ``settings.json`` to the node's
scratch and run them. The renderers here validate the options and assemble those
settings; the work itself is ``analyzer_jobs``' and ``blocksci_jobs``'.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from exporters.artifact_paths import COINJOIN_ANALYSIS_TOOLS_DIR, DUMPLINGS_SNAPSHOT_FILE
from exporters.verify_chain import is_block_hash

from coinjoin_pipeline.configuration import CoinjoinAnalysisConfiguration, MappingsConfiguration
from coinjoin_pipeline.execution.coinjoin_tools import (
    RESEARCH_PROGRAMS,
    WORKFLOW_SCRIPTS,
    command as coinjoin_tools_command,
    command_argv,
    uses_run_input,
)
from coinjoin_pipeline.paths import EXPORTERS_ROOT
from coinjoin_pipeline.registry import pinned_reference
from coinjoin_pipeline.storage.s3 import (
    S3Target,
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
    validate_s3_profile,
)

from .analysis_modes import MODE_OUTPUTS, analysis_mode_settings
from .defaults import BLOCKSCI_IMAGE_PYTHON_COMMAND, DEFAULT_RESOURCES, PBS_MULTIJOB_LOCATIONS, PBSResources
from .scripts import (
    STAGE_RUNTIME_MODULES,
    directives,
    mappings_run,
    launcher,
    package_files,
    template,
    validated_s3_uri,
    write_files,
)
from .validation import (
    PBSError,
    require_existing_path,
    require_notebook_port,
    require_safe_image,
    require_safe_pbs_token,
    require_storage_path,
    walltime_to_seconds,
)

EXECUTION_DIR = Path(__file__).parent.parent
# Standalone helpers some jobs ship besides the worker.
BLOCK_ARCHIVE = EXECUTION_DIR / "block_archive.py"
NOTEBOOK_SERVER = Path(__file__).parent / "templates/blocksci_notebook.py"
EXTERNAL_NETWORKS = {"bitcoin", "bitcoin_testnet", "bitcoin_regtest"}
BLOCKSCI = DEFAULT_RESOURCES["blocksci"]


def _run_id(value: str) -> str:
    try:
        return validate_run_id(value)
    except ValueError as error:
        raise PBSError(str(error)) from error


def _require_external_chain(network: str | None, max_block: int | None) -> str:
    if network is None or network not in EXTERNAL_NETWORKS:
        raise PBSError("External BlockSci network must be bitcoin, bitcoin_testnet, or bitcoin_regtest")
    if isinstance(max_block, bool) or not isinstance(max_block, int) or max_block < 0:
        raise PBSError("External Bitcoin parsing requires a non-negative --blocksci-max-block")
    return network


INDEX_ROOTS = ("/storage/", "/auto/", *(f"/scratch/multijob-{place}/" for place in PBS_MULTIJOB_LOCATIONS))


@dataclass(frozen=True)
class PersistentIndex:
    """A BlockSci index kept in a directory shared by jobs instead of the S3 cache.

    ``index_dir`` is the working copy jobs read and update in place; ``backup_uri`` is the
    S3 prefix of its single backup, written by the backup job and read to restore an
    incomplete working copy. With ``in_scratch`` a reading job restores that backup, and
    the Dumplings copy at ``dumplings_uri``, into its own scratch and leaves index_dir alone.
    With ``restore`` it fills an incomplete index_dir from the backup and leaves it there,
    with the Dumplings copy beside it, for later jobs until it is deleted explicitly.
    """

    index_dir: str
    backup_uri: str | None = None
    in_scratch: bool = False
    restore: bool = False
    dumplings_uri: str | None = None
    dumplings_dir: str | None = None
    storage_report_uri: str | None = None

    def __post_init__(self) -> None:
        if not self.index_dir.startswith(INDEX_ROOTS) or ".." in Path(self.index_dir).parts:
            raise PBSError(
                f"The index directory must be under /storage/ or /scratch/multijob-<location>/: {self.index_dir}"
            )
        if self.backup_uri is not None:
            validated_s3_uri(self.backup_uri, "index backup URI")
        if self.dumplings_uri is not None:
            validated_s3_uri(self.dumplings_uri, "Dumplings URI")
        if self.dumplings_dir is not None and (
            not self.dumplings_dir.startswith(INDEX_ROOTS) or ".." in Path(self.dumplings_dir).parts
        ):
            raise PBSError(
                f"The Dumplings directory must be under /storage/ or /scratch/multijob-<location>/: {self.dumplings_dir}"
            )


def render_coinjoin_analysis_s3_pbs(
    target: S3Target, image: str, command: str, resources: PBSResources = DEFAULT_RESOURCES["analysis"]
) -> str:
    """Render the baseline job that runs coinjoin-analysis over the emulator logs."""
    return render_python_stage("coinjoin-analysis", target, resources, image, {"command": command})


def render_coinjoin_tools_s3_pbs(
    target: S3Target,
    image: str,
    options: CoinjoinAnalysisConfiguration,
    resources: PBSResources = DEFAULT_RESOURCES["analysis"],
) -> str:
    """Run a shipped analyzer CLI on a copy; publish all files apart from the baseline."""
    program = options.program or ""
    files: dict[str, str] = {}
    if program in RESEARCH_PROGRAMS:
        files["coinjoin_research.py"] = (EXPORTERS_ROOT / "coinjoin_research.py").read_text(encoding="utf-8")
        if program == "update_txids_with_wabisator":
            files["wabisator_source.py"] = template("wabisator_source.txt")
    if program in WORKFLOW_SCRIPTS:
        files = {f"{program}.sh": WORKFLOW_SCRIPTS[program].read_text(encoding="utf-8")}
    return render_python_stage(
        "coinjoin-analysis",
        target,
        resources,
        image,
        {
            "program": program,
            "output_directory": COINJOIN_ANALYSIS_TOOLS_DIR,
            "input_uri": validated_s3_uri(options.input_uri, "analysis input URI") if options.input_uri else None,
            "run_input": uses_run_input(options),
            "invocation": {"argv": command_argv(options, target.run_id), "image": image, "input_uri": options.input_uri},
            "files": files,
            "command": "cd /work && " + coinjoin_tools_command(options, target.run_id),
        },
        job="coinjoin-tools",
    )


# coinjoin-analysis names per protocol: --cjtype, the Scanner/ output folder, data/<name>/false_cjtxs.json.
DUMPLINGS_PROTOCOLS = {
    "wasabi2": ("ww2", "wasabi2", "wasabi2"),
    "joinmarket": ("jm", "joinmarket_all", "joinmarket"),
}


def render_coinjoin_analysis_dumplings_s3_pbs(
    target: S3Target,
    image: str,
    *,
    dumplings_uri: str,
    coinjoin_type: str,
    resources: PBSResources = DEFAULT_RESOURCES["analysis"],
) -> str:
    """Render the mainnet baseline: coinjoin-analysis over the published Dumplings copy.

    ``<dumplings_uri>/Scanner/`` is the copy mainnet-sync keeps incrementally and
    ``manifest.json`` (written last) its height and file sizes. The run gets
    ``coinjoin_tx_info.json``, the image's curated ``false_cjtxs.json`` and the manifest as
    ``dumplings-snapshot.json`` for the external comparison.
    """
    uri = validated_s3_uri(dumplings_uri, "Dumplings snapshot URI")
    if coinjoin_type not in DUMPLINGS_PROTOCOLS:
        raise PBSError(f"Dumplings baselines cover wasabi2 and joinmarket, not {coinjoin_type}")
    cjtype, mix_id, false_cjtxs = DUMPLINGS_PROTOCOLS[coinjoin_type]
    return render_python_stage(
        "coinjoin-analysis",
        target,
        resources,
        image,
        {
            "dumplings_uri": uri,
            "mix_id": mix_id,
            "false_cjtxs": false_cjtxs,
            "snapshot_file": DUMPLINGS_SNAPSHOT_FILE,
            "command": f"python -m cj_process.parse_dumplings --cjtype {cjtype} --action process_dumplings "
            "--target-path /work/dumplings",
        },
        job="dumplings-baseline",
    )


def render_mappings_s3_pbs(
    target: S3Target,
    enumerator_image: str,
    sake_image: str,
    settings: MappingsConfiguration = MappingsConfiguration(),
    resources: PBSResources = DEFAULT_RESOURCES["mappings"],
) -> str:
    """Render the Wasabi mappings/Sake stage over S3-backed inputs."""
    require_safe_image(enumerator_image, "enumerator image")
    require_safe_image(sake_image, "Sake image")
    return render_python_stage(
        "coinjoin-mappings",
        target,
        resources,
        None,
        {"script": mappings_run(settings), "enumerator_image": enumerator_image, "sake_image": sake_image},
    )


def render_blocksci_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    include_report: bool = True,
    export_analysis: bool = False,
) -> str:
    """Parse and analyze in one job, publishing the same index archive as the split workflow."""
    return render_python_stage(
        "blocksci",
        target,
        resources,
        image,
        {"command": command, "include_report": include_report, "export_analysis": export_analysis},
    )


def _bitcoin_datadir(path: Path) -> str:
    """A shared Bitcoin Core coin directory the job reads blocks from."""
    bitcoin_path = path.expanduser().resolve()
    require_storage_path(bitcoin_path)
    require_existing_path(bitcoin_path, "external Bitcoin coin directory")
    if not (bitcoin_path / "blocks").is_dir():
        raise PBSError(f"External Bitcoin coin directory must contain blocks/: {bitcoin_path}")
    return str(bitcoin_path)


def _block_archive(bitcoin_blocks_uri: str, network: str, expected_hash: str | None) -> dict[str, object]:
    """A verified bitcoin-block-archive; mainnet must name the block hash the parse ends at."""
    if network == "bitcoin" and expected_hash is None:
        raise PBSError("Mainnet block archives require --blocksci-expected-block-hash at --blocksci-max-block")
    if expected_hash is not None and not is_block_hash(expected_hash):
        raise PBSError("Expected block hash must be 64 lowercase hexadecimal characters")
    return {
        "blocks_uri": validated_s3_uri(bitcoin_blocks_uri, "Bitcoin block archive URI"),
        "expected_hash": expected_hash,
    }


def _parse_source(
    *,
    external_bitcoin_datadir: Path | None,
    bitcoin_blocks_uri: str | None,
    expected_block_hash: str | None,
    external_blocksci_dir: Path | None,
    external_network: str | None,
    external_max_block: int | None,
) -> dict[str, object]:
    """Validate the parse's single source; kind becomes the cache manifest's source_kind."""
    if sum(value is not None for value in (external_bitcoin_datadir, bitcoin_blocks_uri, external_blocksci_dir)) > 1:
        raise PBSError("Choose only one external Bitcoin or BlockSci source")
    if external_bitcoin_datadir is not None:
        return {
            "kind": "external-bitcoin",
            "network": _require_external_chain(external_network, external_max_block),
            "description": "external Bitcoin Core block directory",
            "datadir": _bitcoin_datadir(external_bitcoin_datadir),
            "max_block": external_max_block,
        }
    if bitcoin_blocks_uri is not None:
        network = _require_external_chain(external_network, external_max_block)
        return {
            "kind": "bitcoin-blocks-s3",
            "network": network,
            "description": "verified Bitcoin block archive from S3",
            "max_block": external_max_block,
            **_block_archive(bitcoin_blocks_uri, network, expected_block_hash),
        }
    if external_blocksci_dir is not None:
        blocksci_path = external_blocksci_dir.expanduser().resolve()
        require_storage_path(blocksci_path)
        require_existing_path(blocksci_path, "external BlockSci directory")
        for relative in ("config.json", "parsed/chain/block.dat"):
            if not (blocksci_path / relative).is_file():
                raise PBSError(f"External BlockSci directory must contain {relative}: {blocksci_path}")
        return {
            "kind": "external-blocksci",
            "network": "from-config",
            "description": "existing external BlockSci index",
            "blocksci_dir": str(blocksci_path),
        }
    return {"kind": "emulator", "network": "bitcoin_regtest", "description": "emulator Bitcoin and exported-block inputs"}


def _persistent_settings(index: PersistentIndex | None, walltime: str) -> dict[str, object] | None:
    """Where the persistent index lives and how long its lock entry lives and waits."""
    if index is None:
        return None
    seconds = walltime_to_seconds(walltime)
    return {
        "index_dir": index.index_dir,
        "backup_uri": index.backup_uri,
        "in_scratch": index.in_scratch,
        "restore": index.restore,
        "dumplings_uri": index.dumplings_uri,
        "dumplings_dir": index.dumplings_dir,
        "storage_report_uri": index.storage_report_uri,
        # Past the walltime the job is gone, so its lock entry may be cleared.
        "lock_seconds": seconds + 600,
        "wait_seconds": max(60, seconds // 2),
        "poll_seconds": 60,
    }


def _index_job_settings(resources: PBSResources) -> dict[str, object]:
    """What parse and update jobs record in an index manifest and need to verify a chain."""
    return {
        "pbs_resources": {"ncpus": resources.ncpus, "mem": resources.mem},
        "python_command": BLOCKSCI_IMAGE_PYTHON_COMMAND,
    }


def render_blocksci_parse_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    external_bitcoin_datadir: Path | None = None,
    bitcoin_blocks_uri: str | None = None,
    expected_block_hash: str | None = None,
    external_blocksci_dir: Path | None = None,
    external_network: str | None = None,
    external_max_block: int | None = None,
    persistent: PersistentIndex | None = None,
    multijob: str | None = None,
) -> str:
    """Render a parser-only job that publishes a checksummed reusable index.

    With ``persistent`` the index is parsed straight into that directory and recorded
    there instead of being archived to S3.
    """
    source = _parse_source(
        external_bitcoin_datadir=external_bitcoin_datadir,
        bitcoin_blocks_uri=bitcoin_blocks_uri,
        expected_block_hash=expected_block_hash,
        external_blocksci_dir=external_blocksci_dir,
        external_network=external_network,
        external_max_block=external_max_block,
    )
    if persistent is not None and source["kind"] not in {"external-bitcoin", "bitcoin-blocks-s3"}:
        raise PBSError("A persistent index is parsed from an external Bitcoin datadir or block archive")
    return render_python_stage(
        "blocksci-parse",
        target,
        resources,
        image,
        {
            "command": command,
            "source": source,
            "persistent": _persistent_settings(persistent, resources.walltime),
            **_index_job_settings(resources),
        },
        multijob=multijob,
        extra_files=(BLOCK_ARCHIVE,) if bitcoin_blocks_uri is not None else (),
    )


def render_blocksci_update_s3_pbs(
    target: S3Target,
    source_run_id: str | None,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    external_network: str,
    external_max_block: int,
    external_bitcoin_datadir: Path | None = None,
    bitcoin_blocks_uri: str | None = None,
    expected_block_hash: str | None = None,
    persistent: PersistentIndex | None = None,
    multijob: str | None = None,
) -> str:
    """Render a job that incrementally updates one S3 cache into a fresh run.

    The cache must come from the same source: BlockSci remembers each block by
    its file number and offset, which only that Bitcoin datadir or block
    archive can resolve. With ``persistent`` the job instead updates that index
    in place under the writer lock, first restoring it from the S3 backup
    when the working copy is incomplete.
    """
    if persistent is None:
        source_run_id = _run_id(source_run_id or "")
        if source_run_id == target.run_id:
            raise PBSError("Incremental BlockSci update requires different source and target run IDs")
    elif source_run_id is not None:
        raise PBSError("A persistent index update reads its own index, not a source run")
    network = _require_external_chain(external_network, external_max_block)
    source: dict[str, object] = {"network": network, "max_block": external_max_block}
    if external_bitcoin_datadir is not None and bitcoin_blocks_uri is None:
        source.update(kind="external-bitcoin", datadir=_bitcoin_datadir(external_bitcoin_datadir))
    elif bitcoin_blocks_uri is not None and external_bitcoin_datadir is None:
        source.update(kind="bitcoin-blocks-s3", **_block_archive(bitcoin_blocks_uri, network, expected_block_hash))
    else:
        raise PBSError("Incremental BlockSci update requires exactly one external Bitcoin datadir or block archive")
    return render_python_stage(
        "blocksci-update",
        target,
        resources,
        image,
        {
            "command": command,
            "source": source,
            "source_run_id": source_run_id,
            "artifact_uri": validate_artifact_uri(target.artifact_uri),
            "persistent": _persistent_settings(persistent, resources.walltime),
            **_index_job_settings(resources),
        },
        multijob=multijob,
        extra_files=(BLOCK_ARCHIVE,) if bitcoin_blocks_uri is not None else (),
    )


def render_blocksci_analyze_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    mode: str = "blocksci-analyze",
    source_run_id: str | None = None,
    persistent: PersistentIndex | None = None,
    multijob: str | None = None,
    user_script: Path | None = None,
    external_baseline_uri: str | None = None,
    external_false_cjtxs_uri: str | None = None,
    notebooks_dir: Path | None = None,
    notebook_port: int = 8888,
) -> str:
    """Render analysis, custom-script, or notebook work over a cached index.

    With ``source_run_id`` the index comes read-only from that run's cache while
    outputs, markers, and logs stay under the target run, so several analyses
    can share one published index without touching it. With ``persistent`` the
    index is read in place under the shared lock instead of being downloaded.
    """
    if mode not in MODE_OUTPUTS:
        raise PBSError(f"Unsupported reusable BlockSci mode: {mode}")
    require_notebook_port(notebook_port)
    if source_run_id is not None and persistent is not None:
        raise PBSError("Choose one index source: a source run's cache or a persistent index")
    if mode == "blocksci-analyze" and persistent is not None:
        raise PBSError("BlockSci detect reads emulator data of its own run and cannot use a persistent index")
    if mode == "blocksci-analyze" and source_run_id is not None:
        raise PBSError("BlockSci detect reads emulator data of its own run and cannot use a source cache")
    if external_false_cjtxs_uri is not None and mode != "blocksci-external":
        raise PBSError("A false-positive list belongs to the external BlockSci report")
    if source_run_id is not None:
        source_run_id = _run_id(source_run_id)
        if source_run_id == target.run_id:
            raise PBSError("A read-only source cache must come from a different run")
    settings = analysis_mode_settings(
        mode,
        user_script=user_script,
        external_baseline_uri=external_baseline_uri,
        external_false_cjtxs_uri=external_false_cjtxs_uri,
        notebooks_dir=notebooks_dir,
        notebook_port=notebook_port,
    )
    return render_python_stage(
        mode,
        target,
        resources,
        image,
        {
            **settings,
            "command": command,
            "source_run_id": source_run_id,
            "artifact_uri": validate_artifact_uri(target.artifact_uri),
            "persistent": _persistent_settings(persistent, resources.walltime),
        },
        job="blocksci-analysis",
        multijob=multijob,
        extra_files=(NOTEBOOK_SERVER,) if mode == "blocksci-notebook" else (),
    )


def render_blocksci_index_backup_s3_pbs(
    target: S3Target,
    *,
    persistent: PersistentIndex,
    multijob: str | None = None,
    resources: PBSResources = BLOCKSCI,
) -> str:
    """Render a job that replaces the S3 backup of the working index.

    It only reads the index, so it holds the shared lock: analyses go on and only an update
    waits. The old backup is deleted first; the new one is valid once its S3 size matches the
    uploaded stream and its ``manifest.json`` is written.
    """
    if persistent.backup_uri is None:
        raise PBSError("An index backup needs --blocksci-index-backup-uri")
    return render_python_stage(
        "blocksci-backup",
        target,
        resources,
        None,
        {"persistent": _persistent_settings(persistent, resources.walltime)},
        multijob=multijob,
    )


def render_python_stage(
    stage: str,
    target: S3Target,
    resources: PBSResources,
    image: str | None,
    settings: dict[str, object],
    *,
    job: str | None = None,
    multijob: str | None = None,
    extra_files: tuple[Path, ...] = (),
) -> str:
    """A job that runs ``stage_runtime.py`` on the node in place of a shell template.

    The worker publishes the job's terminal marker; only an absent SCRATCHDIR or python3
    ends the job before it can.
    """
    require_safe_pbs_token(stage, "PBS stage")
    job_settings: dict[str, object] = {
        "stage": stage,
        "job": job or stage,
        "run_id": validate_run_id(target.run_id),
        "run_prefix": validate_artifact_uri(target.run_prefix),
        "endpoint": validate_s3_endpoint_url(target.endpoint_url),
        "credentials": validate_credentials_file(target.credentials_file),
        "profile": validate_s3_profile(target.profile),
        "log_prefix": target.log_prefix,
        # The whole output goes to S3, so no stage needs PBS to copy its output file back.
        "job_log": f"{stage}.pbs.log",
        "ncpus": resources.ncpus,
        **settings,
    }
    if image is not None:
        require_safe_image(image)
        # The job runs the digest the tag names now; index manifests keep the configured
        # name, so a rebuilt image under the same tag still reuses an existing index.
        job_settings["image"] = pinned_reference(image)
        job_settings["image_name"] = image
    files = {
        "settings.json": json.dumps(job_settings, indent=2),
        "run.py": launcher("coinjoin_pipeline.execution.pbs.stage_runtime"),
        **package_files(*STAGE_RUNTIME_MODULES, *extra_files),
    }
    return "\n".join(
        (
            directives(stage.replace("-", "_") + "_s3", resources, multijob),
            "set -euo pipefail",
            'test -n "${SCRATCHDIR:-}" || { echo "SCRATCHDIR is not set" >&2; exit 1; }',
            'JOB="$SCRATCHDIR/.stage-runtime"',
            'mkdir -p "$JOB"',
            write_files("$JOB", files),
            # -s: a coinjoin_pipeline installed in the user's site-packages never shadows the shipped one.
            'exec python3 -s "$JOB/run.py" "$JOB/settings.json"',
            "",
        )
    )


def render_unified_report_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = DEFAULT_RESOURCES["unified_report"],
    *,
    include_mappings: bool = False,
) -> str:
    """Render the S3 report-only job that joins both analyzer outputs."""
    downloads = [
        ".pipeline/exporters",
        "coinjoin_emulator_data",
        "coinjoin-analysis_data",
        "blocksci-analysis_data",
        *(("coinjoin-mappings_data",) if include_mappings else ()),
    ]
    return render_python_stage(
        "unified-report", target, resources, image, {"command": command, "downloads": downloads}
    )
