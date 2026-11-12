"""Image references of the checkout's lock files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from coinjoin_pipeline.paths import CONTAINER_ROOT

if TYPE_CHECKING:
    from coinjoin_pipeline.configuration import PipelineConfiguration


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


def resolve_unified_report_image(args: PipelineConfiguration) -> str:
    """Configured report image, else the lock file."""
    return args.images.unified_report or read_image_lock("unified-report.image")
