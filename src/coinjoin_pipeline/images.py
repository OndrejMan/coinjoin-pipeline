"""Central image-name and version resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re


IMAGE_NAMES = {
    "pipeline": "ghcr.io/ondrejman/coinjoin-pipeline",
    "emulator": "ghcr.io/ondrejman/coinjoin-emulator",
    "coinjoin_analysis": "ghcr.io/ondrejman/coinjoin-analysis",
    "blocksci": "ghcr.io/ondrejman/blocksci-complete",
    "mappings": "ghcr.io/ondrejman/coinjoin-mappings-enumerator",
    "sake": "ghcr.io/ondrejman/coinjoin-mappings-sake",
}
DEFAULT_VERSION = "latest"

TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
IMAGE_RE = re.compile(
    r"^(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?[a-z0-9._-]+(?:/[a-z0-9._-]+)*(?::[A-Za-z0-9_.-]+|@sha256:[0-9a-fA-F]{64})?$"
)


@dataclass(frozen=True)
class Images:
    pipeline: str
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


def resolve_images(version: str | None, overrides: dict[str, str | None]) -> Images:
    effective_version = version or DEFAULT_VERSION
    validate_version(effective_version)
    resolved: dict[str, str] = {}
    for component, name in IMAGE_NAMES.items():
        override = overrides.get(component)
        if override:
            validate_image(override)
            resolved[component] = override
        else:
            resolved[component] = f"{name}:{effective_version}"
    return Images(**resolved)


def all_images_overridden(
    overrides: dict[str, str | None], components: set[str] | None = None,
) -> bool:
    return all(overrides.get(name) for name in (components or set(IMAGE_NAMES)))
