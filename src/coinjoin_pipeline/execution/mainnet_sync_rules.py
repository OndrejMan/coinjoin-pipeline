"""Which settings a mainnet-sync run accepts; mainnet_sync.py renders and submits its jobs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from coinjoin_pipeline.run_ids import validate_run_id
from coinjoin_pipeline.storage.s3 import validate_artifact_uri
from .pbs.defaults import MAINNET_SYNC_RESOURCES, PBSResources
from .pbs.validation import PBSError, require_safe_image, require_safe_pbs_resources
from .pbs_settings import stage_pbs_resources

if TYPE_CHECKING:
    from coinjoin_pipeline.configuration import PipelineConfiguration

# Each job depends on the previous one.
SYNC_STAGES = tuple(MAINNET_SYNC_RESOURCES)

# Top-level settings mainnet-sync reads besides its mainnet.* and per-job pbs.<stage>.* groups.
MAINNET_SYNC_OPTIONS = frozenset(
    {
        "action",
        "run_id",
        "runs_root",
        "dry_run",
        "run_timezone",
        "driver",
        "images.blocksci",
        "images.version",
        "blocksci.index_dir",
        "blocksci.index_backup_uri",
        "artifacts.backend",
        "artifacts.endpoint_url",
        "artifacts.credentials_file",
        "artifacts.profile",
        "pbs.ncpus",
        "pbs.mem",
        "pbs.scratch",
        "pbs.scratch_type",
        "pbs.walltime",
        "pbs.image",
        "pbs.blocksci_image",
    }
)
# Read only for the CoinJoin Explorer baseline (mainnet.explorer_run_id).
EXPLORER_OPTIONS = frozenset(
    {
        "artifacts.uri",
        "explorer_uri",
        "explorer_source",
        "images.coinjoin_analysis",
        "pbs.coinjoin_analysis_image",
    }
)


def configuration_errors(config: PipelineConfiguration) -> list[str]:
    errors: list[str] = []
    stage_groups = tuple(f"pbs.{stage}." for stage in SYNC_STAGES)
    explorer = config.mainnet.explorer_run_id is not None
    allowed = MAINNET_SYNC_OPTIONS | (EXPLORER_OPTIONS if explorer else frozenset())
    groups = (*stage_groups, "pbs.analysis.") if explorer else stage_groups
    for path in sorted(config.provided):
        if path not in allowed and not path.startswith(("mainnet.", *groups)):
            errors.append(f"{path} is not used by mainnet-sync")
    errors.extend(_explorer_errors(config))
    errors.extend(_location_errors(config))
    errors.extend(_working_directory_errors(config))
    if any((config.stages.analysis, config.stages.blocksci, config.stages.mappings)):
        errors.append("mainnet-sync owns its jobs; omit stages.*")
    if config.driver != "docker":
        errors.append("mainnet-sync runs Apptainer PBS jobs; omit driver: kubernetes")
    if config.mainnet.network == "bitcoin" and config.mainnet.scan_start_height is not None:
        errors.append("scan_start_height is only for isolated regtest validation")
    for stage in SYNC_STAGES:
        errors.extend(
            _failure(require_safe_pbs_resources, resources(config, stage), getattr(config.pbs, stage).multijob)
        )
    for image in (config.mainnet.core_image, config.mainnet.dumplings_image):
        errors.extend(_failure(require_safe_image, image))
    return errors


def _explorer_errors(config: PipelineConfiguration) -> list[str]:
    """The Explorer baseline writes its markers under artifacts.uri and its data under explorer_uri."""
    run_id = config.mainnet.explorer_run_id
    if run_id is None:
        return []
    errors = [f"mainnet.explorer_run_id: {message}" for message in _failure(validate_run_id, run_id)]
    if run_id == config.run_id:
        errors.append("mainnet.explorer_run_id must differ from the synchronization's run_id")
    if not config.explorer_uri:
        errors.append("mainnet.explorer_run_id requires explorer_uri")
    errors.extend(f"artifacts.uri: {message}" for message in _failure(validate_artifact_uri, config.artifacts.uri))
    errors.extend(_failure(require_safe_pbs_resources, stage_pbs_resources(config, "analysis")))
    return errors


def _failure(check: Callable[..., object], *values: object) -> list[str]:
    """The message of a validator that raises on a bad value, or nothing."""
    try:
        check(*values)
    except (ValueError, PBSError) as error:
        return [str(error)]
    return []


def _location_errors(config: PipelineConfiguration) -> list[str]:
    """The run ID, the S3 prefixes and the credentials the jobs read them with."""
    errors: list[str] = []
    mainnet = config.mainnet
    for label, value, validator in (
        ("run_id", config.run_id, validate_run_id),
        ("mainnet.state_uri", mainnet.state_uri, validate_artifact_uri),
        ("mainnet.backup_uri", mainnet.backup_uri, validate_artifact_uri),
        ("mainnet.blocks_uri", mainnet.blocks_uri, validate_artifact_uri),
        ("mainnet.dumplings_uri", mainnet.dumplings_uri, validate_artifact_uri),
        ("blocksci.index_backup_uri", config.blocksci.index_backup_uri, validate_artifact_uri),
    ):
        if value is None:
            errors.append(f"mainnet-sync requires {label}")
        else:
            errors.extend(f"{label}: {message}" for message in _failure(validator, value))
    for name in ("endpoint_url", "credentials_file", "profile"):
        if not getattr(config.artifacts, name):
            errors.append(f"mainnet-sync requires artifacts.{name}")
    prefixes = (
        mainnet.state_uri,
        mainnet.backup_uri,
        config.blocksci.index_backup_uri,
        mainnet.dumplings_uri,
        mainnet.blocks_uri,
    )
    present = [value.rstrip("/") for value in prefixes if value]
    if any(a == b or b.startswith(a + "/") for a in present for b in present if a is not b):
        errors.append("The state, Core, index, Dumplings and block prefixes must be distinct, without nesting")
    return errors


def _working_directory_errors(config: PipelineConfiguration) -> list[str]:
    """The Core and index directories share one multijob scratch but must not overlap."""
    root, index = config.mainnet.root, config.blocksci.index_dir
    if not root or not index:
        return ["mainnet-sync requires mainnet.root and blocksci.index_dir"]
    errors: list[str] = []
    for path in (root, index):
        if not Path(path).is_absolute() or ".." in Path(path).parts or len(Path(path).parts) < 3:
            errors.append("Working directories must be absolute, below a parent directory, without '..'")
    if Path(root) == Path(index) or Path(root) in Path(index).parents or Path(index) in Path(root).parents:
        errors.append("Core and index working directories must be separate, without nesting")
    locations = {getattr(config.pbs, stage).multijob for stage in SYNC_STAGES}
    if len(locations) != 1:
        errors.append("All jobs must request the same multijob location")
    for path in (root, index):
        if path.startswith("/scratch/multijob-"):
            location = path.split("/")[2].removeprefix("multijob-")
            if locations != {location}:
                errors.append(f"{path} requires multijob: {location} on all jobs")
    return errors


def resources(config: PipelineConfiguration, stage: str) -> PBSResources:
    return stage_pbs_resources(config, stage, default=MAINNET_SYNC_RESOURCES[stage])
