import sys
from pathlib import Path

import pytest
from config_support import configuration

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))

from coinjoin_pipeline.execution.pbs_settings import (
    resolve_pbs_image,
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
def test_pbs_image_normalizes_registry_references_only(option, reference, expected):
    args = configuration(**({option: reference} if option != "default" else {}))
    default = reference if option == "default" else "docker://fallback:latest"
    assert resolve_pbs_image(args, default, "pbs_blocksci_image") == expected
    assert with_singularity_scheme(reference) == expected


def test_stage_image_override_takes_precedence():
    args = configuration(pbs_image="shared:tag", pbs_blocksci_image="stage:tag")
    assert resolve_pbs_image(args, "fallback:tag", "pbs_blocksci_image") == "docker://stage:tag"


def test_scratch_type_defaults_to_ssd_with_stage_override_before_shared():
    from coinjoin_pipeline.execution.pbs_settings import resolve_unified_report_pbs_resource, stage_pbs_resources

    assert stage_pbs_resources(configuration(), "blocksci")["scratch_type"] == "ssd"
    args = configuration(pbs_scratch_type="local", pbs_analysis_scratch_type="ssd")
    assert stage_pbs_resources(args, "blocksci")["scratch_type"] == "local"
    assert stage_pbs_resources(args, "analysis")["scratch_type"] == "ssd"
    assert resolve_unified_report_pbs_resource(args, "scratch_type", "ssd") == "local"


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
