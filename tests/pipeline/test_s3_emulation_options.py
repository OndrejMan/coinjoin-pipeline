"""The S3 emulation Job options: typed configuration first, the environment as a fallback."""

import json
from dataclasses import replace
from unittest import mock

import pytest

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.configuration import ConfigurationError
from coinjoin_pipeline.context import resolve_configuration
from coinjoin_pipeline.execution import s3_emulation

S3_EMULATE = [
    "emulate",
    "--engine",
    "wasabi",
    "--driver",
    "kubernetes",
    "--artifact-backend",
    "s3",
    "--run-id",
    "run-1",
    "--s3-secret-name",
    "coinjoin-s3",
    "--reuse-namespace",
    "--artifact-uri",
    "s3://bucket/runs",
    "--s3-endpoint-url",
    "https://s3.example.invalid",
    "--s3-credentials-file",
    "/storage/user/.aws/credentials",
    "--s3-profile",
    "coinjoin",
]
ENVIRONMENT = {
    "COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT": "1800",
    "COINJOIN_BTC_NODE_IMAGE": "btc-node:env",
    "COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT": "201",
    "KUBERNETES_IMAGE_PULL_POLICY": "IfNotPresent",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for variable in ENVIRONMENT:
        monkeypatch.delenv(variable, raising=False)


def _controller_environment(config, tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("")
    config = replace(config, scenario=str(scenario), kubernetes=replace(config.kubernetes, kubeconfig=str(kubeconfig)))
    with mock.patch.object(s3_emulation, "apply_s3_emulation_resources") as apply:
        s3_emulation.run_s3_kubernetes_emulation(config)
    manifest = json.loads(apply.call_args.args[0])
    job = next(item for item in manifest["items"] if item["kind"] == "Job")
    controller = next(item for item in job["spec"]["template"]["spec"]["containers"] if item["name"] == "controller")
    return {item["name"]: item.get("value") for item in controller["env"]}


def test_the_environment_fills_unset_options_and_marks_them_provided(monkeypatch) -> None:
    for variable, value in ENVIRONMENT.items():
        monkeypatch.setenv(variable, value)

    config = resolve_configuration(load_configuration(S3_EMULATE))

    assert config.kubernetes.distributor_startup_timeout == 1800
    assert config.kubernetes.btc_node_image == "btc-node:env"
    assert config.kubernetes.btc_node_initial_block_count == 201
    assert config.kubernetes.image_pull_policy == "IfNotPresent"
    assert {
        "kubernetes.distributor_startup_timeout",
        "kubernetes.btc_node_image",
        "kubernetes.btc_node_initial_block_count",
        "kubernetes.image_pull_policy",
    } <= config.provided


def test_an_explicit_option_wins_over_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("COINJOIN_BTC_NODE_IMAGE", "btc-node:env")

    config = resolve_configuration(load_configuration([*S3_EMULATE, "--btc-node-image", "btc-node:cli"]))

    assert config.kubernetes.btc_node_image == "btc-node:cli"


def test_the_environment_is_ignored_outside_the_s3_emulation(monkeypatch) -> None:
    # The experimental Kubernetes path without S3 still reads these variables itself.
    monkeypatch.setenv("COINJOIN_BTC_NODE_IMAGE", "btc-node:env")

    config = resolve_configuration(
        load_configuration(["full-run", "--engine", "wasabi", "--driver", "kubernetes", "--run-id", "run-1"])
    )

    assert config.kubernetes.btc_node_image is None


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT", "abc", "must be an integer"),
        ("COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT", "0", "must be positive"),
        ("COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT", "-1", "must be non-negative"),
        ("KUBERNETES_IMAGE_PULL_POLICY", "Sometimes", "must be one of: Always, IfNotPresent, Never"),
    ],
)
def test_an_invalid_environment_value_names_its_variable(monkeypatch, variable, value, message) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ConfigurationError, match=f"{variable}.*{message}"):
        resolve_configuration(load_configuration(S3_EMULATE))


def test_the_job_renders_the_configured_options_not_the_environment(monkeypatch, tmp_path) -> None:
    config = resolve_configuration(
        load_configuration(
            [
                *S3_EMULATE,
                "--distributor-startup-timeout",
                "1800",
                "--btc-node-image",
                "btc-node:cli",
                "--btc-node-initial-block-count",
                "201",
                "--kubernetes-image-pull-policy",
                "Never",
            ]
        )
    )
    # Set only after resolution: the Job must not read the process environment.
    monkeypatch.setenv("COINJOIN_BTC_NODE_IMAGE", "btc-node:env")

    environment = _controller_environment(config, tmp_path)

    assert environment["COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT"] == "1800"
    assert environment["BTC_NODE_IMAGE"] == "btc-node:cli"
    assert environment["COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT"] == "201"
    assert environment["KUBERNETES_IMAGE_PULL_POLICY"] == "Never"


def test_unset_options_leave_the_emulator_defaults(tmp_path) -> None:
    environment = _controller_environment(resolve_configuration(load_configuration(S3_EMULATE)), tmp_path)

    for variable in (
        "COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT",
        "BTC_NODE_IMAGE",
        "COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT",
        "KUBERNETES_IMAGE_PULL_POLICY",
    ):
        assert variable not in environment
