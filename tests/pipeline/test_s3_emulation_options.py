"""The S3 emulation Job options: only the typed configuration sets them, never the environment."""

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


def test_the_environment_no_longer_sets_the_options(monkeypatch) -> None:
    for variable, value in ENVIRONMENT.items():
        monkeypatch.setenv(variable, value)

    config = resolve_configuration(load_configuration(S3_EMULATE))

    assert config.kubernetes.distributor_startup_timeout is None
    assert config.kubernetes.btc_node_image is None
    assert config.kubernetes.btc_node_initial_block_count is None
    assert config.kubernetes.image_pull_policy is None


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--btc-node-initial-block-count", "-1"),
        ("--distributor-startup-timeout", "0"),
        ("--kubernetes-image-pull-policy", "Sometimes"),
    ],
)
def test_an_invalid_option_value_is_refused(option, value) -> None:
    with pytest.raises((ConfigurationError, SystemExit)):
        resolve_configuration(load_configuration([*S3_EMULATE, option, value]))


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
