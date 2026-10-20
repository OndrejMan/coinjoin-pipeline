"""Pure PBS resource and image-reference resolution helpers."""

from __future__ import annotations

import os
from dataclasses import fields

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.pbs.defaults import (
    DEFAULT_PBS_IMAGES,
    DEFAULT_RESOURCES,
    PBS_QUEUE_MARGIN_SECONDS,
    PBSResources,
)
from coinjoin_pipeline.execution.pbs.validation import walltime_to_seconds
from coinjoin_pipeline.paths import CONTAINER_ROOT


def truthy_env(name: str) -> bool:
    """Interpret the conventional false-like environment values as false."""
    return os.environ.get(name, "").lower() not in ("", "0", "false", "no")


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


def pbs_image(args: PipelineConfiguration, component: str) -> str:
    """The Singularity reference of one component: its own override, the shared one, the default."""
    image = getattr(args.pbs, f"{component}_image") or args.pbs.image or DEFAULT_PBS_IMAGES[component]
    return with_singularity_scheme(image)


def read_image_lock(name: str) -> str:
    """Read a committed image reference from the checkout's container/ dir."""
    path = CONTAINER_ROOT / name
    try:
        reference = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"image lock file is unreadable: {path}") from error
    if not reference:
        raise RuntimeError(f"image lock file is empty: {path}")
    return reference


def resolve_uploader_image(args: PipelineConfiguration) -> str:
    """Configured uploader image (``COINJOIN_UPLOADER_IMAGE`` is folded in), else the lock file."""
    return args.images.uploader or read_image_lock("uploader.image")


def resolve_unified_report_image(args: PipelineConfiguration) -> str:
    """Configured report image (``COINJOIN_UNIFIED_REPORT_IMAGE`` is folded in), else the lock file."""
    return args.images.unified_report or read_image_lock("unified-report.image")


def stage_pbs_resources(args: PipelineConfiguration, group: str) -> PBSResources:
    """Resolve one resource group: stage override, then the shared ``pbs.*`` value, then the default."""
    sources = (getattr(args.pbs, group), args.pbs, DEFAULT_RESOURCES[group])

    def pick(name: str) -> object:
        return next(value for source in sources if (value := getattr(source, name)) is not None)

    return PBSResources(**{item.name: pick(item.name) for item in fields(PBSResources)})  # type: ignore[arg-type]


def pbs_wait_timeout(walltime: str) -> int:
    """Return the stage walltime plus the established one-hour queue margin."""
    return walltime_to_seconds(walltime) + PBS_QUEUE_MARGIN_SECONDS
