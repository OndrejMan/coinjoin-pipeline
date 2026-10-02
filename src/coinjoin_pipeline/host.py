"""Resolved container image selection."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .images import IMAGE_NAMES, Images, validate_image


def image_overrides(host: Mapping[str, object]) -> dict[str, str | None]:
    environment_names = {
        "emulator": "COINJOIN_EMULATOR_IMAGE",
        "coinjoin_analysis": "COINJOIN_ANALYSIS_IMAGE",
        "blocksci": "BLOCKSCI_IMAGE",
        "mappings": "MAPPINGS_ENUMERATOR_IMAGE",
        "sake": "SAKE_IMAGE",
    }
    return {
        component: (str(host[component]) if host.get(component) else os.environ.get(environment_names[component]))
        for component in IMAGE_NAMES
    }


def local_images(overrides: Mapping[str, str | None] | None = None) -> Images:
    defaults = Images(
        emulator="coinjoin-emulator:local",
        coinjoin_analysis="coinjoin-analysis:local",
        blocksci="blocksci-complete:local",
        mappings="coinjoin-mappings-enumerator:local",
        sake="coinjoin-mappings-sake:local",
    ).as_dict()
    for component in defaults:
        override = (overrides or {}).get(component)
        if override:
            validate_image(override)
            defaults[component] = override
    return Images(**defaults)
