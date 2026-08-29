"""Pure PBS resource and image-reference resolution helpers."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TypedDict, TypeVar, cast

from client.pbs import (
    DEFAULT_BLOCKSCI_MEM,
    DEFAULT_BLOCKSCI_NCPUS,
    DEFAULT_BLOCKSCI_SCRATCH,
    DEFAULT_BLOCKSCI_WALLTIME,
    DEFAULT_COINJOIN_ANALYSIS_MEM,
    DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
    walltime_to_seconds,
)


def truthy_env(name: str) -> bool:
    """Interpret the conventional false-like environment values as false."""
    return os.environ.get(name, "").lower() not in ("", "0", "false", "no")


def resolve_pbs_image(args: argparse.Namespace, default_image: str, stage_option: str) -> str:
    """Resolve a stage override before the shared PBS image override."""
    stage_image = getattr(args, stage_option, None)
    if stage_image:
        return str(stage_image)
    if getattr(args, "pbs_image", None):
        return str(args.pbs_image)
    return default_image


CONTAINER_LOCK_DIR = Path(__file__).resolve().parents[2] / "container"


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


def resolve_uploader_image(args: argparse.Namespace | None = None) -> str:
    """Resolve uploader image from explicit flag, environment, then lock file."""
    explicit = getattr(args, "uploader_image", None) if args else None
    return explicit or os.environ.get("COINJOIN_UPLOADER_IMAGE") or read_image_lock("uploader.image")


IMAGE_URI_SCHEMES = (
    "docker://", "docker-archive:", "docker-daemon:", "oci:", "oci-archive:",
    "library://", "shub://", "oras://", "http://", "https://", "file://",
)


def with_singularity_scheme(image: str) -> str:
    """Prefix a bare registry reference with ``docker://``, leave URIs alone."""
    return image if image.startswith(IMAGE_URI_SCHEMES) else f"docker://{image}"


def unified_report_image_reference(args: argparse.Namespace | None = None) -> str:
    """Resolve the neutral report-image reference used for provenance."""
    explicit = getattr(args, "unified_report_image", None) if args else None
    return (
        explicit
        or os.environ.get("COINJOIN_UNIFIED_REPORT_IMAGE")
        or read_image_lock("unified-report.image")
    )


def resolve_unified_report_pbs_image(args: argparse.Namespace | None = None) -> str:
    """Resolve the report image in the URI spelling needed by Singularity."""
    return with_singularity_scheme(unified_report_image_reference(args))


PBSResource = TypeVar("PBSResource", int, str)


class PBSResources(TypedDict):
    """Resolved scheduler resources for one PBS stage."""

    ncpus: int
    mem: str
    scratch: str
    walltime: str


def resolve_pbs_resource(args: argparse.Namespace, name: str, default: PBSResource) -> PBSResource:
    """Use a shared PBS override when present, otherwise the supplied default."""
    value = getattr(args, name, None)
    return default if value is None else cast(PBSResource, value)


def resolve_stage_pbs_resource(
    args: argparse.Namespace,
    stage: str,
    name: str,
    default: PBSResource,
) -> PBSResource:
    """Resolve a stage-specific value before the shared PBS fallback."""
    stage_value = getattr(args, f"pbs_{stage}_{name}", None)
    if stage_value is not None:
        return cast(PBSResource, stage_value)
    return resolve_pbs_resource(args, f"pbs_{name}", default)


def resolve_unified_report_pbs_resource(
    args: argparse.Namespace,
    name: str,
    default: PBSResource,
) -> PBSResource:
    """Resolve a report-specific override before the shared PBS fallback."""
    report_value = getattr(args, f"pbs_unified_report_{name}", None)
    if report_value is not None:
        return cast(PBSResource, report_value)
    return resolve_pbs_resource(args, f"pbs_{name}", default)


def stage_pbs_resources(args: argparse.Namespace, stage: str) -> PBSResources:
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


def pbs_wait_timeout(walltime: str) -> int:
    """Return the stage walltime plus the established one-hour queue margin."""
    return walltime_to_seconds(walltime) + 60 * 60
