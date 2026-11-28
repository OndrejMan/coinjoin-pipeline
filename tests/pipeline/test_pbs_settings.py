import sys
from pathlib import Path

import pytest
from config_support import configuration

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))

from coinjoin_pipeline.execution.pbs_settings import (
    pbs_image,
    stage_pbs_resources,
    with_singularity_scheme,
)


# The default image comes from RunContext (images.blocksci), covered by the registry test below.
@pytest.mark.parametrize("option", ["pbs.blocksci_image", "pbs.image"])
@pytest.mark.parametrize(
    "reference,expected",
    [
        (
            "ghcr.io/ondrejman/blocksci-complete:latest",
            "docker://ghcr.io/ondrejman/blocksci-complete:latest",
        ),
        ("docker://registry/image:tag", "docker://registry/image:tag"),
        ("docker-archive:/storage/image.tar", "docker-archive:/storage/image.tar"),
        ("oras://registry/image:tag", "oras://registry/image:tag"),
        ("/storage/image.sif", "/storage/image.sif"),
        ("./images/blocksci.sif", "./images/blocksci.sif"),
        ("blocksci.sif", "blocksci.sif"),
    ],
)
def test_pbs_image_normalizes_registry_references_only(option, reference, expected):
    args = configuration({option: reference})
    assert pbs_image(args, "blocksci") == expected
    assert with_singularity_scheme(reference) == expected


def test_stage_image_override_takes_precedence():
    args = configuration({"pbs.image": "shared:tag", "pbs.blocksci_image": "stage:tag"})
    assert pbs_image(args, "blocksci") == "docker://stage:tag"


def test_scratch_type_defaults_to_ssd_with_stage_override_before_shared():
    assert stage_pbs_resources(configuration(), "blocksci").scratch_type == "ssd"
    args = configuration({"pbs.scratch_type": "local", "pbs.analysis.scratch_type": "ssd"})
    assert stage_pbs_resources(args, "blocksci").scratch_type == "local"
    assert stage_pbs_resources(args, "analysis").scratch_type == "ssd"
    assert stage_pbs_resources(args, "unified_report").scratch_type == "local"


def test_stage_resources_fall_back_from_stage_to_shared_to_default():
    args = configuration({"pbs.ncpus": 6, "pbs.blocksci.ncpus": 12, "pbs.unified_report.walltime": "03:00:00"})
    assert stage_pbs_resources(args, "blocksci").ncpus == 12
    assert stage_pbs_resources(args, "analysis").ncpus == 6
    report = stage_pbs_resources(args, "unified_report")
    assert (report.ncpus, report.mem, report.walltime) == (6, "8gb", "03:00:00")


def test_scratch_type_is_a_yaml_choice(tmp_path):
    from coinjoin_pipeline.arguments import load_configuration
    from coinjoin_pipeline.configuration import ConfigurationError, PipelineConfiguration

    assert PipelineConfiguration.from_paths({"pbs.blocksci.scratch_type": "local"}).pbs.blocksci.scratch_type == "local"
    with pytest.raises(ConfigurationError):
        PipelineConfiguration.from_paths({"pbs.scratch_type": "shm"})
    experiment = tmp_path / "experiment.yaml"
    experiment.write_text("action: full-run\npbs:\n  scratch_type: tmp\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_configuration(["run", str(experiment)])


def test_default_pbs_image_follows_the_configured_registry(monkeypatch):
    monkeypatch.delenv("COINJOIN_IMAGE_REGISTRY", raising=False)
    args = configuration({"images.registry": "ghcr.io/fork"})
    assert pbs_image(args, "sake") == "docker://ghcr.io/fork/coinjoin-mappings-sake:latest"
