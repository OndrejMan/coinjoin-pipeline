"""Pure PBS resource and Singularity image-reference resolution helpers."""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING

from coinjoin_pipeline.configuration import required
from coinjoin_pipeline.execution.pbs.defaults import (
    DEFAULT_RESOURCES,
    PBS_QUEUE_MARGIN_SECONDS,
    PBSResources,
)
from coinjoin_pipeline.execution.pbs.validation import walltime_to_seconds

if TYPE_CHECKING:
    from coinjoin_pipeline.configuration import PipelineConfiguration


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
    """The Singularity reference of one component, which RunContext resolves for every run."""
    image = getattr(args.pbs, f"{component}_image") or args.pbs.image
    return with_singularity_scheme(required(image, f"pbs.{component}_image"))


def stage_pbs_resources(
    args: PipelineConfiguration, group: str, *, default: PBSResources | None = None
) -> PBSResources:
    """Resolve one resource group: stage override, then the shared ``pbs.*`` value, then the default.

    ``default`` replaces the group's ``DEFAULT_RESOURCES`` entry for jobs with their own budget.
    """
    sources = (getattr(args.pbs, group), args.pbs, default or DEFAULT_RESOURCES[group])

    def pick(name: str) -> object:
        return next(value for source in sources if (value := getattr(source, name)) is not None)

    return PBSResources(**{item.name: pick(item.name) for item in fields(PBSResources)})  # type: ignore[arg-type]


def pbs_wait_timeout(walltime: str) -> int:
    """Return the stage walltime plus the established one-hour queue margin."""
    return walltime_to_seconds(walltime) + PBS_QUEUE_MARGIN_SECONDS
