from pathlib import Path

import pytest
import yaml

from coinjoin_pipeline import PipelineConfiguration as ExportedPipelineConfiguration
from coinjoin_pipeline.configuration import (
    ArtifactConfiguration,
    ConfigurationError,
    PBSResourceConfiguration,
    PipelineConfiguration,
    configuration_arguments,
    expand_configuration,
)
from coinjoin_pipeline.commands import action_from, validate_passthrough
from coinjoin_pipeline.host import parse_host_options


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


def test_pipeline_configuration_is_exported_from_package() -> None:
    assert ExportedPipelineConfiguration is PipelineConfiguration


ADVANCED_CONFIG = """\
action: pbs-from-s3
runtime: podman
runs_root: /storage/user/coinjoin-runs
engine: wasabi
coinjoin_type: wasabi2
run_id: advanced-run-1
emulation_timeout: 21600

artifacts:
  backend: s3
  uri: s3://bucket/runs
  endpoint_url: https://s3.example.invalid
  credentials_file: /storage/user/.aws/credentials
  profile: coinjoin

images:
  version: thesis-2026-07
  pipeline: registry/pipeline:test
  emulator: registry/emulator:test
  coinjoin_analysis: registry/analysis:test
  blocksci: registry/blocksci:test
  mappings: registry/mappings:test
  sake: registry/sake:test

blocksci:
  workflow: reusable
  task: parse
  cache_source_run_id: source-run
  notebook_port: 8888
  notebooks_dir: /storage/user/notebooks
  external_bitcoin_datadir: /storage/user/bitcoin
  external_blocksci_dir: /storage/user/blocksci
  network: bitcoin
  max_block: 850000

joinmarket:
  detector: possible
  min_base_fee: 5000
  percentage_fee: 0.00004
  max_depth: 200000

mappings:
  mining_fee_rate: 1
  coordination_fee_rate: 0.003
  max_decomposition_fee: 6000
  mode: all
  timeout: 90
  retry_timeout: 900
  sake_seed: 20260704

stages:
  blocksci: true
  mappings: true

pbs:
  image: docker://registry/shared:test
  blocksci_image: docker://registry/blocksci:test
  coinjoin_analysis_image: docker://registry/analysis:test
  mappings_enumerator_image: docker://registry/mappings:test
  sake_image: docker://registry/sake:test
  bitcoin_datadir: /storage/user/bitcoin
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


def test_advanced_schema_maps_every_supported_section(tmp_path: Path) -> None:
    path = tmp_path / "advanced.yaml"
    path.write_text(ADVANCED_CONFIG, encoding="utf-8")

    configuration = PipelineConfiguration.from_yaml(path)
    arguments = configuration.to_arguments()

    assert configuration.runtime == "podman"
    assert configuration.blocksci.workflow == "reusable"
    assert configuration.blocksci.task == "parse"
    assert configuration.blocksci.external_bitcoin_datadir == "/storage/user/bitcoin"
    assert configuration.blocksci.external_blocksci_dir == "/storage/user/blocksci"
    assert configuration.blocksci.max_block == 850000
    assert configuration.joinmarket.detector == "possible"
    assert configuration.joinmarket.percentage_fee == 0.00004
    assert configuration.mappings.timeout == 90
    assert configuration.mappings.retry_timeout == 900
    assert configuration.emulation_timeout == 21600
    assert configuration.images.blocksci == "registry/blocksci:test"
    assert configuration.pbs.blocksci_image == "docker://registry/blocksci:test"

    expected_pairs = {
        "--blocksci-workflow": "reusable",
        "--blocksci-task": "parse",
        "--blocksci-external-bitcoin-datadir": "/storage/user/bitcoin",
        "--blocksci-external-blocksci-dir": "/storage/user/blocksci",
        "--blocksci-max-block": "850000",
        "--joinmarket-detector": "possible",
        "--joinmarket-percentage-fee": "4e-05",
        "--mapping-timeout": "90",
        "--mapping-retry-timeout": "900",
        "--emulation-timeout": "21600",
        "--blocksci-image": "registry/blocksci:test",
        "--pbs-blocksci-image": "docker://registry/blocksci:test",
    }
    for flag, expected in expected_pairs.items():
        assert arguments[arguments.index(flag) + 1] == expected
    assert "--blocksciPbs" in arguments
    assert "--mappingsPbs" in arguments


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
        (
            "blocksci:\n  workflow: resume\n",
            "blocksci.workflow must be one of",
        ),
        (
            "mappings:\n  timeout: '60'\n",
            "mappings.timeout must be a positive integer",
        ),
        (
            "images:\n  local_build: 'true'\n",
            "images.local_build must be true or false",
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


def test_external_analysis_configuration_matches_cli_contract(tmp_path: Path) -> None:
    path = tmp_path / "external.yaml"
    path.write_text(
        """\
action: external-analyze
runtime: podman
runs_root: /storage/user/coinjoin-runs
run_id: mainnet-2026
coinjoin_type: joinmarket
dry_run: true

images:
  blocksci: registry.example/blocksci:thesis

external:
  bitcoin_datadir: /storage/user/bitcoin
  baseline: /storage/user/coinjoin_tx_info.json
  false_cjtxs:
    - /storage/user/false_cjtxs.json
    - /storage/user/false_cjtxs.json.1
  network: bitcoin
  min_free_gb: 250
""",
        encoding="utf-8",
    )

    configuration = PipelineConfiguration.from_yaml(path)
    arguments = configuration.to_arguments()

    assert configuration.action == "external analyze"
    assert configuration.external.false_cjtxs == (
        "/storage/user/false_cjtxs.json",
        "/storage/user/false_cjtxs.json.1",
    )
    assert arguments[:2] == ["external", "analyze"]
    assert arguments.count("--false-cjtxs") == 2
    assert arguments[arguments.index("--min-free-gb") + 1] == "250"
    passthrough, host = parse_host_options(arguments)
    assert host["runtime"] == "podman"
    action = action_from(passthrough)
    assert action == "external analyze"
    assert validate_passthrough(passthrough, action) == []


def test_external_analysis_resume_configuration_and_cli_override(
    tmp_path: Path,
) -> None:
    path = tmp_path / "external-resume.yaml"
    path.write_text(
        """\
action: external analyze
run_id: mainnet-2026
external:
  resume: true
  min_free_gb: 100
""",
        encoding="utf-8",
    )

    arguments, selected = expand_configuration(
        ["--from-configuration", str(path), "--min-free-gb", "200"]
    )

    assert selected == path.resolve()
    assert arguments[:2] == ["external", "analyze"]
    assert "--resume" in arguments
    assert arguments.count("--min-free-gb") == 1
    assert arguments[arguments.index("--min-free-gb") + 1] == "200"


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        (
            "action: external analyze\nrun_id: x\nexternal:\n  baseline: /tmp/b.json\n",
            "requires external.bitcoin_datadir and external.baseline",
        ),
        (
            "action: external analyze\nrun_id: x\nexternal:\n  resume: true\n  baseline: /tmp/b.json\n",
            "external.resume cannot be combined",
        ),
        (
            "action: full-run\nexternal:\n  min_free_gb: 20\n",
            "external settings require action",
        ),
        (
            "action: external analyze\nrun_id: x\nexternal:\n  resume: true\n  false_cjtxs: [one, 2]\n",
            "external.false_cjtxs must be a string or a list of strings",
        ),
    ],
)
def test_external_analysis_configuration_validation(
    yaml_text: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        PipelineConfiguration.from_mapping(yaml.safe_load(yaml_text))


def test_configuration_rejects_explicit_external_cli_action(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text("engine: wasabi\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="put the action in YAML"):
        expand_configuration(
            ["--from-configuration", str(path), "external", "analyze"]
        )
