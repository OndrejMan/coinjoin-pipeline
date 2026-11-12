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


@pytest.mark.parametrize("option", ["pbs_blocksci_image", "pbs_image", "default"])
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
def test_pbs_image_normalizes_registry_references_only(monkeypatch, option, reference, expected):
    from coinjoin_pipeline.execution import pbs_settings

    args = configuration(**({option: reference} if option != "default" else {}))
    if option == "default":
        monkeypatch.setitem(pbs_settings.DEFAULT_PBS_IMAGES, "blocksci", reference)
    assert pbs_image(args, "blocksci") == expected
    assert with_singularity_scheme(reference) == expected


def test_stage_image_override_takes_precedence():
    args = configuration(pbs_image="shared:tag", pbs_blocksci_image="stage:tag")
    assert pbs_image(args, "blocksci") == "docker://stage:tag"


def test_scratch_type_defaults_to_ssd_with_stage_override_before_shared():
    assert stage_pbs_resources(configuration(), "blocksci").scratch_type == "ssd"
    args = configuration(pbs_scratch_type="local", pbs_analysis_scratch_type="ssd")
    assert stage_pbs_resources(args, "blocksci").scratch_type == "local"
    assert stage_pbs_resources(args, "analysis").scratch_type == "ssd"
    assert stage_pbs_resources(args, "unified_report").scratch_type == "local"


def test_stage_resources_fall_back_from_stage_to_shared_to_default():
    args = configuration(pbs_ncpus=6, pbs_blocksci_ncpus=12, pbs_unified_report_walltime="03:00:00")
    assert stage_pbs_resources(args, "blocksci").ncpus == 12
    assert stage_pbs_resources(args, "analysis").ncpus == 6
    report = stage_pbs_resources(args, "unified_report")
    assert (report.ncpus, report.mem, report.walltime) == (6, "8gb", "03:00:00")


def test_scratch_type_is_a_cli_and_yaml_choice(tmp_path):
    from coinjoin_pipeline.arguments import load_configuration
    from coinjoin_pipeline.configuration import ConfigurationError

    assert load_configuration(["--pbs-blocksci-scratch-type", "local"]).pbs.blocksci.scratch_type == "local"
    with pytest.raises(SystemExit):
        load_configuration(["--pbs-scratch-type", "shm"])
    experiment = tmp_path / "experiment.yaml"
    experiment.write_text("action: full-run\npbs:\n  scratch_type: tmp\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_configuration(["run", str(experiment)])
