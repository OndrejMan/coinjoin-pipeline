"""Central image-name and version resolution."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass

IMAGE_NAMES = {
    "emulator": "ghcr.io/ondrejman/emulator-manager",
    "coinjoin_analysis": "ghcr.io/ondrejman/coinjoin-analysis",
    "blocksci": "ghcr.io/ondrejman/blocksci-complete",
    "mappings": "ghcr.io/ondrejman/coinjoin-mappings-enumerator",
    "sake": "ghcr.io/ondrejman/coinjoin-mappings-sake",
}
# What ./run-all.sh local tags its images as.
LOCAL_IMAGES = {
    "emulator": "coinjoin-emulator:local",
    "coinjoin_analysis": "coinjoin-analysis:local",
    "blocksci": "blocksci-complete:local",
    "mappings": "coinjoin-mappings-enumerator:local",
    "sake": "coinjoin-mappings-sake:local",
}
IMAGE_ENVIRONMENT = {
    "emulator": "COINJOIN_EMULATOR_IMAGE",
    "coinjoin_analysis": "COINJOIN_ANALYSIS_IMAGE",
    "blocksci": "BLOCKSCI_IMAGE",
    "mappings": "MAPPINGS_ENUMERATOR_IMAGE",
    "sake": "SAKE_IMAGE",
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
    mappings: str
    sake: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def validate_version(version: str) -> None:
    if not TAG_RE.fullmatch(version):
        raise ValueError(f"invalid image version tag: {version!r}")


def validate_image(image: str) -> None:
    if not IMAGE_RE.fullmatch(image):
        raise ValueError(f"invalid container image reference: {image!r}")


def resolve_images(version: str | None, overrides: dict[str, str | None], *, local: bool = False) -> Images:
    """Each component's configured image, else its environment variable, else the release or local tag."""
    effective_version = version or DEFAULT_VERSION
    validate_version(effective_version)
    resolved: dict[str, str] = {}
    for component, name in IMAGE_NAMES.items():
        override = overrides.get(component) or os.environ.get(IMAGE_ENVIRONMENT[component])
        if override:
            validate_image(override)
            resolved[component] = override
        else:
            resolved[component] = LOCAL_IMAGES[component] if local else f"{name}:{effective_version}"
    return Images(**resolved)
