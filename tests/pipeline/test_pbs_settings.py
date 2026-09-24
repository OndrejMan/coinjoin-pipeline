import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))

from client.pbs_settings import resolve_pbs_image, with_singularity_scheme


@pytest.mark.parametrize("option", ["pbs_blocksci_image", "pbs_image", "default"])
@pytest.mark.parametrize("reference,expected", [
    ("ghcr.io/ondrejman/blocksci-complete:latest",
     "docker://ghcr.io/ondrejman/blocksci-complete:latest"),
    ("docker://registry/image:tag", "docker://registry/image:tag"),
    ("docker-archive:/storage/image.tar", "docker-archive:/storage/image.tar"),
    ("oras://registry/image:tag", "oras://registry/image:tag"),
    ("/storage/image.sif", "/storage/image.sif"),
    ("./images/blocksci.sif", "./images/blocksci.sif"),
    ("blocksci.sif", "blocksci.sif"),
])
def test_pbs_image_normalizes_registry_references_only(option, reference, expected):
    args = argparse.Namespace()
    default = reference if option == "default" else "docker://fallback:latest"
    if option != "default":
        setattr(args, option, reference)
    assert resolve_pbs_image(args, default, "pbs_blocksci_image") == expected
    assert with_singularity_scheme(reference) == expected


def test_stage_image_override_takes_precedence():
    args = argparse.Namespace(pbs_image="shared:tag", pbs_blocksci_image="stage:tag")
    assert resolve_pbs_image(args, "fallback:tag", "pbs_blocksci_image") == "docker://stage:tag"
