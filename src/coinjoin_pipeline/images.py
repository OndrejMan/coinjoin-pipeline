"""Central image-name and version resolution."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .paths import CONTAINER_ROOT

if TYPE_CHECKING:
    from .configuration import PipelineConfiguration

# Where the project's images are published; a fork publishing its own sets
# images.registry (--image-registry, $COINJOIN_IMAGE_REGISTRY).
DEFAULT_REGISTRY = "ghcr.io/ondrejman"
REGISTRY_ENVIRONMENT = "COINJOIN_IMAGE_REGISTRY"
REPOSITORIES = {
    "emulator": "emulator-manager",
    "coinjoin_analysis": "coinjoin-analysis",
    "blocksci": "blocksci-complete",
}


def image_registry(configured: str | None = None) -> str:
    """The configured registry, else ``$COINJOIN_IMAGE_REGISTRY``, else the project's own."""
    registry = (configured or os.environ.get(REGISTRY_ENVIRONMENT) or DEFAULT_REGISTRY).rstrip("/")
    validate_image(registry)
    return registry


def image_names(registry: str | None = None) -> dict[str, str]:
    """Each component's image name, without a tag, in that registry."""
    prefix = image_registry(registry)
    return {component: f"{prefix}/{repository}" for component, repository in REPOSITORIES.items()}


def in_registry(image: str, registry: str | None) -> str:
    """A default image of the project's registry, moved to the configured one."""
    prefix = f"{DEFAULT_REGISTRY}/"
    return image_registry(registry) + "/" + image[len(prefix) :] if image.startswith(prefix) else image


# The tags a local build of each component's repository produces.
LOCAL_IMAGES = {
    "emulator": "coinjoin-emulator:local",
    "coinjoin_analysis": "coinjoin-analysis:local",
    "blocksci": "blocksci-complete:local",
}
IMAGE_ENVIRONMENT = {
    "emulator": "COINJOIN_EMULATOR_IMAGE",
    "coinjoin_analysis": "COINJOIN_ANALYSIS_IMAGE",
    "blocksci": "BLOCKSCI_IMAGE",
}
DEFAULT_VERSION = "latest"

TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
IMAGE_RE = re.compile(
    r"^(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?[a-z0-9._-]+(?:/[a-z0-9._-]+)*(?::[A-Za-z0-9_.-]+|@sha256:[0-9a-fA-F]{64})?$"
)


@dataclass(frozen=True)
class Images:
    emulator: str
    coinjoin_analysis: str
    blocksci: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def validate_version(version: str) -> None:
    if not TAG_RE.fullmatch(version):
        raise ValueError(f"invalid image version tag: {version!r}")


def validate_image(image: str) -> None:
    if not IMAGE_RE.fullmatch(image):
        raise ValueError(f"invalid container image reference: {image!r}")


def resolve_images(
    version: str | None,
    overrides: dict[str, str | None],
    *,
    local: bool = False,
    registry: str | None = None,
) -> Images:
    """Each component's configured image, else its environment variable, else the release or local tag."""
    effective_version = version or DEFAULT_VERSION
    validate_version(effective_version)
    resolved: dict[str, str] = {}
    for component, name in image_names(registry).items():
        override = overrides.get(component) or os.environ.get(IMAGE_ENVIRONMENT[component])
        if override:
            validate_image(override)
            resolved[component] = override
        else:
            resolved[component] = LOCAL_IMAGES[component] if local else f"{name}:{effective_version}"
    return Images(**resolved)


def default_image(component: str, registry: str | None = None) -> str:
    """The released image of one component in that registry, resolved when it is needed.

    A module constant would fix the registry at import, before a configuration names one.
    """
    return f"{image_names(registry)[component]}:{DEFAULT_VERSION}"


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
    """Configured uploader image, else the lock file in the configured registry."""
    return args.images.uploader or in_registry(read_image_lock("uploader.image"), args.images.registry)


def resolve_unified_report_image(args: PipelineConfiguration) -> str:
    """Configured report image, else the lock file."""
    return args.images.unified_report or read_image_lock("unified-report.image")
