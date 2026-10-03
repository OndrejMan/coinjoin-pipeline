from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.configuration import ConfigurationError, PipelineConfiguration
from coinjoin_pipeline.context import resolve_configuration


@pytest.mark.parametrize(
    "data",
    [
        {"unknown": True},
        {"blocksci": {"unknown": 1}},
        {"stages": {"analysis": "yes"}},
        {"pbs": {"analysis": {"ncpus": True}}},
        {"pbs": {"analysis": {"ncpus": 0}}},
        {"engine": "unknown"},
        {"min_input_count": 0},
        {"joinmarket": {"percentage_fee": -1}},
        {"joinmarket": {"percentage_fee": float("nan")}},
        {"action": ["full-run"]},
        {"engine": None},
        {"joinmarket": {"max_depth": None}},
    ],
)
def test_rejects_invalid_values(data):
    with pytest.raises(ConfigurationError):
        PipelineConfiguration.from_mapping(data)


def test_null_is_accepted_only_where_the_field_is_optional():
    config = PipelineConfiguration.from_mapping({"scenario": None, "min_input_count": None})
    assert (config.scenario, config.min_input_count, config.engine) == (None, None, "wasabi")
    with pytest.raises(ConfigurationError, match="runs_root is required"):
        PipelineConfiguration().runs_path


def test_cli_override_changes_values_without_rendering_arguments(tmp_path):
    path = tmp_path / "experiment.yaml"
    path.write_text("engine: wasabi\nmin_input_count: 15\njoinmarket:\n  max_depth: 100\n")
    config = load_configuration(["run", str(path), "--engine", "joinmarket", "--joinmarket-max-depth", "200"])
    assert config.engine == "joinmarket"
    assert config.min_input_count == 15
    assert config.joinmarket.max_depth == 200
    with pytest.raises(FrozenInstanceError):
        config.engine = "wasabi"


def test_shorthand_actions_may_follow_options(tmp_path):
    path = tmp_path / "experiment.yaml"
    path.write_text("action: full-run\nengine: wasabi\n")
    config = load_configuration(["--runs-root", str(tmp_path), "--version", "v1", "run", str(path), "--dry-run"])
    assert (config.action, config.runs_root, config.images.version, config.dry_run) == (
        "full-run",
        str(tmp_path),
        "v1",
        True,
    )
    external = load_configuration(["--runtime", "podman", "external", "analyze", "--run-id", "ext-1", "--resume"])
    assert (external.action, external.runtime) == ("external analyze", "podman")
    # A value that happens to spell an action is not the action word.
    assert load_configuration(["--run-id", "run", "emulate"]).action == "emulate"


def test_resources_enable_the_selected_pbs_stage():
    config = resolve_configuration(PipelineConfiguration.from_mapping({"pbs": {"analysis": {"ncpus": 4}}}))
    assert config.stages.analysis
    assert not config.stages.blocksci
    assert config.pbs.analysis.ncpus == 4


def test_explicit_disabled_stage_rejects_attached_resources():
    config = PipelineConfiguration.from_mapping({"stages": {"analysis": False}, "pbs": {"analysis": {"ncpus": 4}}})
    with pytest.raises(ConfigurationError, match="resources require"):
        resolve_configuration(config)


def test_default_namespace_only_requires_kubernetes_when_explicit():
    resolve_configuration(PipelineConfiguration.from_mapping({"action": "emulate"}))
    config = PipelineConfiguration.from_mapping({"action": "emulate", "kubernetes": {"namespace": "coinjoin"}})
    with pytest.raises(ConfigurationError, match="--namespace requires --driver kubernetes"):
        resolve_configuration(config)


def test_explicit_false_kubernetes_switches_do_not_require_kubernetes():
    config = PipelineConfiguration.from_mapping(
        {"action": "emulate", "kubernetes": {"copy_to_host": False, "reuse_namespace": False}}
    )
    assert resolve_configuration(config).driver == "docker"


def test_zero_is_a_supplied_maximum_height():
    config = PipelineConfiguration.from_mapping(
        {
            "action": "pbs-from-s3",
            "run_id": "genesis",
            "stages": {"blocksci": True},
            "blocksci": {
                "workflow": "reusable",
                "task": "parse",
                "bitcoin_blocks_uri": "s3://blocks/mainnet",
                "network": "bitcoin",
                "max_block": 0,
                "expected_block_hash": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
            },
            "artifacts": {
                "uri": "s3://bucket/runs",
                "endpoint_url": "https://s3.example",
                "credentials_file": "/tmp/credentials",
                "profile": "test",
            },
        }
    )
    assert resolve_configuration(config).blocksci.max_block == 0


def test_effective_env_defaults_are_validated(monkeypatch):
    monkeypatch.setenv("PBS_BITCOIN_DATADIR", "/tmp/btc")
    config = PipelineConfiguration.from_mapping(
        {
            "action": "emulate",
            "driver": "kubernetes",
            "run_id": "test",
            "kubernetes": {"reuse_namespace": True},
            "artifacts": {
                "backend": "s3",
                "uri": "s3://bucket/runs",
                "endpoint_url": "https://s3.example",
                "secret_name": "secret",
                "credentials_file": "/tmp/credentials",
                "profile": "test",
            },
        }
    )
    with pytest.raises(ConfigurationError, match="shared Bitcoin"):
        resolve_configuration(config)


@pytest.mark.parametrize(
    "name",
    [
        "wasabi-s3-no-mappings.yaml",
        "wasabi-s3-pbs-no-mappings.yaml",
        "metacentrum-mainnet-s3-blocks-parse.yaml",
        "metacentrum-mainnet-dumplings-report.yaml",
    ],
)
def test_existing_examples_load_directly(name):
    root = Path(__file__).resolve().parents[2]
    config = resolve_configuration(load_configuration(["run", str(root / "examples" / name), "--dry-run"]))
    assert config.dry_run
    assert config.artifacts.backend == "s3"


def test_no_explicit_action_alongside_yaml(tmp_path):
    path = tmp_path / "experiment.yaml"
    path.write_text("engine: wasabi\n")
    with pytest.raises(ConfigurationError, match="put the action in YAML"):
        load_configuration(["full-run", "--from-configuration", str(path)])


def test_external_resume_rejects_new_inputs():
    config = PipelineConfiguration.from_mapping(
        {
            "action": "external analyze",
            "external": {"resume": True, "baseline": "baseline.json"},
        }
    )
    with pytest.raises(ConfigurationError, match="cannot be combined"):
        resolve_configuration(config)


def test_alias_and_repeated_values_keep_cli_order():
    config = load_configuration(["coinjoin", "--run-dir", "run-a", "--run-id=old", "--run-id", "new"])
    assert config.action == "coinjoin-analysis"
    assert config.run_id == "new"
