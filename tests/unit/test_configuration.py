from pathlib import Path

import pytest

from coinjoin_pipeline.configuration import (
    ArtifactConfiguration,
    ConfigurationError,
    PBSResourceConfiguration,
    PipelineConfiguration,
    configuration_arguments,
    expand_configuration,
)


CONFIG = """\
engine: wasabi
coinjoin_type: wasabi2
driver: kubernetes

kubernetes:
  namespace: man5-ns
  reuse_namespace: true

artifacts:
  backend: s3
  uri: s3://coinjoin-thesis/runs
  endpoint_url: https://s3.cl4.du.cesnet.cz
  secret_name: coinjoin-s3-credentials
  credentials_file: /storage/brno2/home/xman/.aws/credentials
  profile: coinjoin

stages:
  mappings: true

pbs:
  blocksci:
    ncpus: 32
    mem: 2tb
    scratch: 2tb
    walltime: "48:00:00"
  analysis:
    ncpus: 8
    mem: 32gb
    scratch: 100gb
    walltime: "08:00:00"
"""


def test_example_configuration_flattens_to_public_cli(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    arguments = configuration_arguments(path)

    assert arguments[0] == "full-run"
    assert "--reuse-namespace" in arguments
    assert "--analysisPbs" in arguments
    assert "--blocksciPbs" in arguments
    assert "--mappingsPbs" in arguments
    assert arguments[arguments.index("--pbs-blocksci-mem") + 1] == "2tb"
    assert arguments[arguments.index("--pbs-analysis-mem") + 1] == "32gb"
    assert "--run-id" not in arguments


def test_yaml_loads_typed_pipeline_configuration(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    configuration = PipelineConfiguration.from_yaml(path)

    assert configuration.engine == "wasabi"
    assert configuration.driver == "kubernetes"
    assert isinstance(configuration.artifacts, ArtifactConfiguration)
    assert configuration.artifacts.backend == "s3"
    assert isinstance(configuration.pbs.blocksci, PBSResourceConfiguration)
    assert configuration.pbs.blocksci.ncpus == 32
    assert configuration.pbs.blocksci.mem == "2tb"
    assert configuration.stages.mappings is True
    assert configuration.to_arguments() == configuration_arguments(path)


def test_shared_pbs_resources_are_directly_typed_on_pbs_model() -> None:
    configuration = PipelineConfiguration.from_mapping(
        {"pbs": {"ncpus": 4, "mem": "24gb", "walltime": "04:00:00"}}
    )

    assert configuration.pbs.ncpus == 4
    assert configuration.pbs.mem == "24gb"
    assert configuration.pbs.walltime == "04:00:00"
    assert configuration.to_arguments() == [
        "full-run",
        "--pbs-ncpus",
        "4",
        "--pbs-mem",
        "24gb",
        "--pbs-walltime",
        "04:00:00",
    ]


def test_cli_value_overrides_configuration(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    arguments, selected = expand_configuration(
        ["--fromConfiguration", str(path), "--pbs-blocksci-mem", "1tb"]
    )

    assert selected == path.resolve()
    assert arguments.count("--pbs-blocksci-mem") == 1
    assert arguments[arguments.index("--pbs-blocksci-mem") + 1] == "1tb"


def test_unknown_keys_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text(
        "engine: wasabi\npbs:\n  blockcsi:\n    mem: 2tb\n", encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match="unsupported pbs key"):
        configuration_arguments(path)


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("engine: 12\n", "configuration.engine must be one of"),
        ("dry_run: 'yes'\n", "configuration.dry_run must be true or false"),
        (
            "pbs:\n  blocksci:\n    ncpus: '32'\n",
            "pbs.blocksci.ncpus must be a positive integer",
        ),
        (
            "artifacts:\n  backend: filesystem\n",
            "artifacts.backend must be one of",
        ),
    ],
)
def test_typed_configuration_rejects_wrong_value_types(
    tmp_path: Path, yaml_text: str, message: str
) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        PipelineConfiguration.from_yaml(path)


def test_configuration_rejects_explicit_cli_action(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text("engine: wasabi\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="put the action in YAML"):
        expand_configuration(["--from-configuration", str(path), "recreate"])
