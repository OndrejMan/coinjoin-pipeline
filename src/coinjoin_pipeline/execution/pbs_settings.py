"""Pure PBS resource and image-reference resolution helpers."""

from __future__ import annotations

import os
from typing import TypedDict, TypeVar, cast

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.pbs.defaults import (
    DEFAULT_BLOCKSCI_MEM,
    DEFAULT_BLOCKSCI_NCPUS,
    DEFAULT_BLOCKSCI_SCRATCH,
    DEFAULT_BLOCKSCI_WALLTIME,
    DEFAULT_COINJOIN_ANALYSIS_MEM,
    DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
    DEFAULT_UNIFIED_REPORT_WALLTIME,
    PBS_QUEUE_MARGIN_SECONDS,
)
from coinjoin_pipeline.execution.pbs.validation import walltime_to_seconds
from coinjoin_pipeline.paths import CONTAINER_ROOT


def truthy_env(name: str) -> bool:
    """Interpret the conventional false-like environment values as false."""
    return os.environ.get(name, "").lower() not in ("", "0", "false", "no")


def resolve_pbs_image(args: PipelineConfiguration, default_image: str, stage_option: str) -> str:
    """Resolve a stage override before the shared PBS image override."""
    stage_image = getattr(args.pbs, stage_option.removeprefix("pbs_"), None)
    if stage_image:
        return with_singularity_scheme(str(stage_image))
    if args.pbs.image:
        return with_singularity_scheme(str(args.pbs.image))
    return with_singularity_scheme(default_image)


CONTAINER_LOCK_DIR = CONTAINER_ROOT


def read_image_lock(name: str) -> str:
    """Read a committed image reference from the checkout's container/ dir."""
    path = CONTAINER_LOCK_DIR / name
    try:
        reference = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"image lock file is unreadable: {path}") from error
    if not reference:
        raise RuntimeError(f"image lock file is empty: {path}")
    return reference


def resolve_uploader_image(args: PipelineConfiguration) -> str:
    """Resolve the uploader image from configuration, then the lock file.

    ``COINJOIN_UPLOADER_IMAGE`` is folded into the configuration when it is resolved.
    """
    return args.images.uploader or read_image_lock("uploader.image")


IMAGE_URI_SCHEMES = (
    "docker://",
    "docker-archive:",
    "docker-daemon:",
    "oci:",
    "oci-archive:",
    "library://",
    "shub://",
    "oras://",
    "http://",
    "https://",
    "file://",
)


def with_singularity_scheme(image: str) -> str:
    """Prefix registry references while preserving transports and local images."""
    if image.startswith(IMAGE_URI_SCHEMES + ("/", "./", "../")) or image.endswith((".sif", ".simg")):
        return image
    return f"docker://{image}"


def unified_report_image_reference(args: PipelineConfiguration) -> str:
    """Resolve the neutral report-image reference used for provenance.

    ``COINJOIN_UNIFIED_REPORT_IMAGE`` is folded into the configuration when it is resolved.
    """
    return args.images.unified_report or read_image_lock("unified-report.image")


def resolve_unified_report_pbs_image(args: PipelineConfiguration) -> str:
    """Resolve the report image in the URI spelling needed by Singularity."""
    return with_singularity_scheme(unified_report_image_reference(args))


PBSResource = TypeVar("PBSResource", int, str)


class PBSResources(TypedDict):
    """Resolved scheduler resources for one PBS stage."""

    ncpus: int
    mem: str
    scratch: str
    walltime: str


def resolve_pbs_resource(args: PipelineConfiguration, name: str, default: PBSResource) -> PBSResource:
    """Use a shared PBS override when present, otherwise the supplied default."""
    value = getattr(args.pbs, name.removeprefix("pbs_"), None)
    return default if value is None else cast(PBSResource, value)


def resolve_stage_pbs_resource(
    args: PipelineConfiguration,
    stage: str,
    name: str,
    default: PBSResource,
) -> PBSResource:
    """Resolve a stage-specific value before the shared PBS fallback."""
    stage_value = getattr(getattr(args.pbs, stage), name)
    if stage_value is not None:
        return cast(PBSResource, stage_value)
    return resolve_pbs_resource(args, f"pbs_{name}", default)


def resolve_unified_report_pbs_resource(
    args: PipelineConfiguration,
    name: str,
    default: PBSResource,
) -> PBSResource:
    """Resolve a report-specific override before the shared PBS fallback."""
    report_value = getattr(args.pbs.unified_report, name)
    if report_value is not None:
        return cast(PBSResource, report_value)
    return resolve_pbs_resource(args, f"pbs_{name}", default)


def stage_pbs_resources(args: PipelineConfiguration, stage: str) -> PBSResources:
    """Resolve the four resource values for one named PBS analysis stage."""
    if stage == "blocksci":
        defaults = (
            DEFAULT_BLOCKSCI_NCPUS,
            DEFAULT_BLOCKSCI_MEM,
            DEFAULT_BLOCKSCI_SCRATCH,
            DEFAULT_BLOCKSCI_WALLTIME,
        )
    elif stage in {"analysis", "mappings"}:
        defaults = (
            DEFAULT_COINJOIN_ANALYSIS_NCPUS,
            DEFAULT_COINJOIN_ANALYSIS_MEM,
            DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
            DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
        )
    else:
        raise ValueError(f"Unsupported PBS resource stage: {stage}")
    ncpus, mem, scratch, walltime = defaults
    return {
        "ncpus": resolve_stage_pbs_resource(args, stage, "ncpus", ncpus),
        "mem": resolve_stage_pbs_resource(args, stage, "mem", mem),
        "scratch": resolve_stage_pbs_resource(args, stage, "scratch", scratch),
        "walltime": resolve_stage_pbs_resource(args, stage, "walltime", walltime),
    }


def stage_pbs_walltime(args: PipelineConfiguration, group: str) -> str:
    """Resolve the walltime of one planned stage from its resource group.

    The unified report has its own override namespace; every other group
    resolves through the shared stage/PBS fallback chain.
    """
    if group == "report":
        return cast(
            str,
            resolve_unified_report_pbs_resource(args, "walltime", DEFAULT_UNIFIED_REPORT_WALLTIME),
        )
    return cast(str, stage_pbs_resources(args, group)["walltime"])


def pbs_wait_timeout(walltime: str) -> int:
    """Return the stage walltime plus the established one-hour queue margin."""
    return walltime_to_seconds(walltime) + PBS_QUEUE_MARGIN_SECONDS
