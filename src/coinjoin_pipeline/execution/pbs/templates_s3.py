"""S3-backed PBS job renderers.

Every job is ``directives`` and a few lines of shell that write the stage worker's
modules (``scripts.STAGE_RUNTIME_MODULES``) and a ``settings.json`` to the node's
scratch and run them. The renderers here validate the options and assemble those
settings; the work itself is ``analyzer_jobs``', ``blocksci_jobs``' and
``blocksci_analysis_jobs``'.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, Union

from exporters.artifact_paths import (
    BLOCKSCI_ANALYSIS_DIR,
    COINJOIN_ANALYSIS_DIR,
    EMULATOR_DIR,
    MAPPINGS_DIR,
    STAGED_EXPORTERS_DIR,
)

from coinjoin_pipeline.configuration import (
    BlockSciConfiguration,
    MappingsConfiguration,
    required,
)
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
from .defaults import BLOCKSCI_IMAGE_PYTHON_COMMAND, DEFAULT_RESOURCES, PBSResources
from .scripts import (
    STAGE_RUNTIME_MODULES,
    directives,
    mappings_run,
    launcher,
    package_files,
    validated_s3_uri,
    write_files,
)
from .stage_settings import (
    BlocksciAnalysisSettings,
    BlocksciParseSettings,
    BlocksciSettings,
    BlocksciUpdateSettings,
    CommandSettings,
    MappingsSettings,
    PbsResourceSettings,
    PersistentJobSettings,
    PersistentSettings,
    SourceSettings,
    StageSettings,
    UnifiedReportSettings,
)
from .validation import (
    PBSError,
    require_existing_path,
    require_safe_image,
    require_safe_pbs_token,
    require_storage_path,
    walltime_to_seconds,
)

EXECUTION_DIR = Path(__file__).parent.parent
# Standalone helpers some jobs ship besides the worker.
BLOCK_ARCHIVE = EXECUTION_DIR / "block_archive.py"
NOTEBOOK_SERVER = Path(__file__).parent / "templates/blocksci_notebook.py"
BLOCKSCI = DEFAULT_RESOURCES["blocksci"]
# The fields one job adds to the common StageSettings; its job function reads them.
JobSettings = Union[
    CommandSettings,
    UnifiedReportSettings,
    MappingsSettings,
    BlocksciSettings,
    BlocksciParseSettings,
    BlocksciUpdateSettings,
    BlocksciAnalysisSettings,
    PersistentJobSettings,
]


def render_coinjoin_analysis_s3_pbs(
    target: S3Target, image: str, command: str, resources: PBSResources = DEFAULT_RESOURCES["analysis"]
) -> str:
    """Render the baseline job that runs coinjoin-analysis over the emulator logs."""
    return render_python_stage("coinjoin-analysis", target, resources, image, {"command": command})


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


def _block_source(blocksci: BlockSciConfiguration) -> SourceSettings:
    """The single block source of a parse or update; kind becomes the cache manifest's source_kind.

    The configuration rules allow one source and require its network and last height;
    an update takes only a Bitcoin datadir or a block archive.
    """
    if blocksci.external_bitcoin_datadir:
        return {
            "kind": "external-bitcoin",
            "network": required(blocksci.network, "--blocksci-network"),
            "description": "external Bitcoin Core block directory",
            "datadir": _bitcoin_datadir(Path(blocksci.external_bitcoin_datadir)),
            "max_block": required(blocksci.max_block, "--blocksci-max-block"),
        }
    if blocksci.bitcoin_blocks_uri:
        return {
            "kind": "bitcoin-blocks-s3",
            "network": required(blocksci.network, "--blocksci-network"),
            "description": "verified Bitcoin block archive from S3",
            "max_block": required(blocksci.max_block, "--blocksci-max-block"),
            "blocks_uri": validated_s3_uri(blocksci.bitcoin_blocks_uri, "Bitcoin block archive URI"),
            "expected_hash": blocksci.expected_block_hash,
        }
    if blocksci.external_blocksci_dir:
        blocksci_path = Path(blocksci.external_blocksci_dir).expanduser().resolve()
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
    return {
        "kind": "emulator",
        "network": "bitcoin_regtest",
        "description": "emulator Bitcoin and exported-block inputs",
    }


def _persistent_settings(blocksci: BlockSciConfiguration, walltime: str) -> PersistentSettings | None:
    """A BlockSci index kept in a directory shared by jobs; None when the job uses the S3 cache.

    ``index_dir`` is the working copy jobs read and update in place; ``backup_uri`` is the
    S3 prefix of its single backup, written by the backup job and read to restore an
    incomplete working copy.
    """
    if not blocksci.index_dir:
        return None
    seconds = walltime_to_seconds(walltime)
    return {
        "index_dir": blocksci.index_dir,
        "backup_uri": blocksci.index_backup_uri,
        # Past the walltime the job is gone, so its lock entry may be cleared.
        "lock_seconds": seconds + 600,
        "wait_seconds": max(60, seconds // 2),
        "poll_seconds": 60,
    }


class _IndexJobSettings(TypedDict):
    pbs_resources: PbsResourceSettings
    python_command: str


def _index_job_settings(resources: PBSResources) -> _IndexJobSettings:
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
    blocksci: BlockSciConfiguration = BlockSciConfiguration(),
    multijob: str | None = None,
) -> str:
    """Render a parser-only job that publishes a checksummed reusable index.

    With ``blocksci.index_dir`` the index is parsed straight into that directory and
    recorded there instead of being archived to S3.
    """
    return render_python_stage(
        "blocksci-parse",
        target,
        resources,
        image,
        {
            "command": command,
            "source": _block_source(blocksci),
            "persistent": _persistent_settings(blocksci, resources.walltime),
            **_index_job_settings(resources),
        },
        multijob=multijob,
        extra_files=(BLOCK_ARCHIVE,) if blocksci.bitcoin_blocks_uri else (),
    )


def render_blocksci_update_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    blocksci: BlockSciConfiguration,
    multijob: str | None = None,
) -> str:
    """Render a job that incrementally updates one S3 cache into a fresh run.

    The cache must come from the same source: BlockSci remembers each block by
    its file number and offset, which only that Bitcoin datadir or block
    archive can resolve. With ``blocksci.index_dir`` the job instead updates that
    index in place under the writer lock, first restoring it from the S3 backup
    when the working copy is incomplete.
    """
    return render_python_stage(
        "blocksci-update",
        target,
        resources,
        image,
        {
            "command": command,
            "source": _block_source(blocksci),
            "source_run_id": blocksci.cache_source_run_id,
            "artifact_uri": validate_artifact_uri(target.artifact_uri),
            "persistent": _persistent_settings(blocksci, resources.walltime),
            **_index_job_settings(resources),
        },
        multijob=multijob,
        extra_files=(BLOCK_ARCHIVE,) if blocksci.bitcoin_blocks_uri else (),
    )


def render_blocksci_analyze_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    mode: str = "blocksci-analyze",
    blocksci: BlockSciConfiguration = BlockSciConfiguration(),
    multijob: str | None = None,
) -> str:
    """Render analysis, custom-script, or notebook work over a cached index.

    With ``blocksci.cache_source_run_id`` the index comes read-only from that run's cache
    while outputs, markers, and logs stay under the target run, so several analyses
    can share one published index without touching it. With ``blocksci.index_dir`` the
    index is read in place under the shared lock instead of being downloaded.
    """
    if mode not in MODE_OUTPUTS:
        raise PBSError(f"Unsupported reusable BlockSci mode: {mode}")
    return render_python_stage(
        mode,
        target,
        resources,
        image,
        {
            **analysis_mode_settings(mode, blocksci),
            "command": command,
            "source_run_id": blocksci.cache_source_run_id,
            "artifact_uri": validate_artifact_uri(target.artifact_uri),
            "persistent": _persistent_settings(blocksci, resources.walltime),
        },
        job="blocksci-analysis",
        multijob=multijob,
        extra_files=(NOTEBOOK_SERVER,) if mode == "blocksci-notebook" else (),
    )


def render_blocksci_index_backup_s3_pbs(
    target: S3Target,
    *,
    blocksci: BlockSciConfiguration,
    multijob: str | None = None,
    resources: PBSResources = BLOCKSCI,
) -> str:
    """Render a job that replaces the S3 backup of the working index.

    It only reads the index, so it holds the shared lock: analyses go on and only an update
    waits. The old backup is deleted first; the new one is valid once its S3 size matches the
    uploaded stream and its ``manifest.json`` is written.
    """
    return render_python_stage(
        "blocksci-backup",
        target,
        resources,
        None,
        {"persistent": _persistent_settings(blocksci, resources.walltime)},
        multijob=multijob,
    )


def render_python_stage(
    stage: str,
    target: S3Target,
    resources: PBSResources,
    image: str | None,
    settings: JobSettings,
    *,
    job: str | None = None,
    multijob: str | None = None,
    extra_files: tuple[Path, ...] = (),
) -> str:
    """The job script: write the worker's modules and settings.json to scratch and run ``stage_runtime``.

    The worker publishes the job's terminal marker; only an absent SCRATCHDIR or python3
    ends the job before it can.
    """
    require_safe_pbs_token(stage, "PBS stage")
    common: StageSettings = {
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
    }
    image_settings: dict[str, str] = {}
    if image is not None:
        require_safe_image(image)
        # The job runs the digest the tag names now; index manifests keep the configured
        # name, so a rebuilt image under the same tag still reuses an existing index.
        image_settings = {"image": pinned_reference(image), "image_name": image}
    files = {
        # One flat object: the worker reads it as StageSettings and as the job's own type.
        "settings.json": json.dumps({**common, **settings, **image_settings}, indent=2),
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
        STAGED_EXPORTERS_DIR,
        EMULATOR_DIR,
        COINJOIN_ANALYSIS_DIR,
        BLOCKSCI_ANALYSIS_DIR,
        *((MAPPINGS_DIR,) if include_mappings else ()),
    ]
    return render_python_stage("unified-report", target, resources, image, {"command": command, "downloads": downloads})
