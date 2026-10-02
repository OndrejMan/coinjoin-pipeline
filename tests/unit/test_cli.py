import json
import os
from pathlib import Path
from unittest import mock

import pytest

from coinjoin_pipeline import cli
from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.context import RunContext
from coinjoin_pipeline.doctor import (
    Capability,
    required_capabilities,
    required_image_components,
)
from coinjoin_pipeline.images import resolve_images
from coinjoin_pipeline.manifest import redact
from coinjoin_pipeline.runs import run_id_for
from coinjoin_pipeline.storage.s3 import validate_run_id


def test_executes_typed_configuration_in_process_and_restores_environment(tmp_path):
    observed = []

    def execute(context):
        observed.append(context)
        assert os.environ["PIPELINE_RUN_ID"] == "run-a"
        assert context.config.engine == "joinmarket"
        assert context.config.coinjoin_type == "joinmarket"

    with (
        mock.patch.dict(os.environ, {"PIPELINE_RUN_ID": "outside"}),
        mock.patch.object(cli, "doctor_check", return_value=[]),
        mock.patch.object(cli, "execute", side_effect=execute),
    ):
        assert (
            cli.main(
                [
                    "full-run",
                    "--engine",
                    "joinmarket",
                    "--runs-root",
                    str(tmp_path),
                    "--run-id",
                    "run-a",
                ]
            )
            == 0
        )
        assert os.environ["PIPELINE_RUN_ID"] == "outside"
    data = json.loads((tmp_path / "run-a/research_manifest.json").read_text())
    assert data["mode"] == "emulator"
    assert data["run_id"] == "run-a"
    assert data["host_launcher"]["status"] == "completed"
    assert data["host_launcher"]["configuration"]["engine"] == "joinmarket"
    assert len(observed) == 1


def test_failed_execution_finishes_manifest(tmp_path):
    with (
        mock.patch.object(cli, "doctor_check", return_value=[]),
        mock.patch.object(cli, "execute", side_effect=RuntimeError("failed stage")),
    ):
        assert cli.main(["emulate", "--runs-root", str(tmp_path), "--run-id", "failed-run"]) == 5
    manifest = json.loads((tmp_path / "failed-run/research_manifest.json").read_text())
    assert manifest["host_launcher"]["status"] == "failed"
    assert manifest["host_launcher"]["exit_code"] == 5


@pytest.mark.parametrize("fails", [False, True])
def test_manifest_records_effective_environment_without_secrets(tmp_path, fails):
    inherited = {
        "COINJOIN_BTC_NODE_IMAGE": "btc-node:review",
        "COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT": "173",
        "KUBERNETES_IMAGE_PULL_POLICY": "IfNotPresent",
        "BLOCKSCI_IMAGE": "blocksci:inherited",
        "COINJOIN_API_TOKEN": "private-token",
        "PBS_PASSWORD": "private-password",
        "KUBERNETES_CLIENT_SECRET": "private-secret",
        "MAPPINGS_ACCESS_KEY_ID": "private-access-key",
        "SAKE_PRIVATE_KEY": "private-key",
        "COINJOIN_REGISTRY_CREDENTIALS": "private-credentials",
        "UNRELATED_REVIEW_SETTING": "unrelated-value",
    }
    target = tmp_path / "run-a/research_manifest.json"
    observed = []

    def execute(_context):
        prepared = json.loads(target.read_text())["host_launcher"]
        assert prepared["status"] == "prepared"
        environment = prepared["environment"]
        assert environment["COINJOIN_BTC_NODE_IMAGE"] == os.environ["COINJOIN_BTC_NODE_IMAGE"]
        assert environment["COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT"] == "173"
        assert environment["KUBERNETES_IMAGE_PULL_POLICY"] == "IfNotPresent"
        assert environment["BLOCKSCI_IMAGE"] == os.environ["BLOCKSCI_IMAGE"] == "blocksci:explicit"
        observed.append(environment)
        if fails:
            raise RuntimeError("failed stage")

    with (
        mock.patch.dict(os.environ, inherited, clear=True),
        mock.patch.object(cli, "doctor_check", return_value=[]),
        mock.patch.object(cli, "execute", side_effect=execute),
    ):
        code = cli.main(
            ["emulate", "--runs-root", str(tmp_path), "--run-id", "run-a", "--blocksci-image", "blocksci:explicit"]
        )
        assert code == (5 if fails else 0)
        assert dict(os.environ) == inherited
    text = target.read_text()
    finished = json.loads(text)["host_launcher"]
    assert finished["environment"] == observed[0]
    assert finished["status"] == ("failed" if fails else "completed")
    for key, value in inherited.items():
        if value.startswith("private-") or key == "UNRELATED_REVIEW_SETTING":
            assert key not in finished["environment"]
            assert value not in text


def test_dry_run_does_not_overwrite_manifest_or_preflight_runtime(tmp_path):
    run = tmp_path / "run-a"
    run.mkdir()
    manifest = run / "research_manifest.json"
    manifest.write_text('{"evidence": true}')
    with (
        mock.patch.object(cli, "doctor_check") as doctor,
        mock.patch.object(cli, "execute") as execute,
    ):
        assert (
            cli.main(
                [
                    "full-run",
                    "--runs-root",
                    str(tmp_path),
                    "--run-id",
                    "run-a",
                    "--dry-run",
                ]
            )
            == 0
        )
    doctor.assert_not_called()
    assert execute.call_args.args[0].config.dry_run
    assert manifest.read_text() == '{"evidence": true}'
    assert not (tmp_path / ".notebooks").exists()


@pytest.mark.parametrize(
    "action,module",
    [
        ("watch", "watch"),
        ("download-report", "download_report"),
        ("clean-s3", "clean_s3"),
    ],
)
def test_utility_routes_without_container_preflight(action, module, tmp_path):
    with (
        mock.patch(f"coinjoin_pipeline.{module}.main", return_value=0) as handler,
        mock.patch.object(cli, "doctor_check") as doctor,
    ):
        assert cli.main(["--runs-root", str(tmp_path), action, "--run-id", "run-a"]) == 0
    doctor.assert_not_called()
    assert handler.call_args.args[0] == ["--run-id", "run-a"]
    assert handler.call_args.kwargs["runs_root"] == tmp_path


@pytest.mark.parametrize("action", ["analyze", "export", "coinjoin-analysis", "mappings"])
def test_stage_requires_selected_run(action):
    with mock.patch.object(cli, "execute") as execute:
        assert cli.main([action, "--dry-run"]) == 2
    execute.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        ["clean"],
        ["full-run", "--test-values"],
        ["full-run", "--min-input-count", "0"],
        ["builder"],
    ],
)
def test_invalid_input_never_executes(args):
    with mock.patch.object(cli, "execute") as execute:
        assert cli.main([*args, "--dry-run"] if args[0] != "clean" else args) == 2
    execute.assert_not_called()


def test_images_resolve_once_before_pbs_rendering():
    config = load_configuration(
        [
            "full-run",
            "--analysisPbs",
            "--blocksciPbs",
            "--version",
            "stable",
            "--blocksci-image",
            "registry/blocksci:chosen",
        ]
    )
    context = RunContext.prepare(config, "cjp")
    assert context.config.pbs.blocksci_image == "registry/blocksci:chosen"
    assert context.config.pbs.coinjoin_analysis_image.endswith(":stable")
    assert context.environment["BLOCKSCI_IMAGE"] == context.config.images.blocksci


def test_explicit_pbs_image_override_wins():
    context = RunContext.prepare(
        load_configuration(
            [
                "full-run",
                "--blocksciPbs",
                "--pbs-blocksci-image",
                "docker://registry/blocksci:pbs",
            ]
        ),
        "cjp",
    )
    assert context.config.pbs.blocksci_image == "docker://registry/blocksci:pbs"


def test_capabilities_follow_actual_delegation():
    config = load_configuration(["full-run", "--analysisPbs"])
    assert "emulator" in required_image_components(config)
    assert "coinjoin_analysis" not in required_image_components(config)
    assert {
        Capability.QSUB,
        Capability.QSTAT,
        Capability.QDEL,
        Capability.CONTAINER_RUNTIME,
    } <= required_capabilities(config)


def test_s3_capabilities_need_no_local_container():
    config = load_configuration(["run", str(Path(__file__).parents[2] / "examples/wasabi-s3-no-mappings.yaml")])
    assert required_image_components(config) == set()
    assert Capability.CONTAINER_RUNTIME not in required_capabilities(config)
    assert {
        Capability.QSUB,
        Capability.QSTAT,
        Capability.QDEL,
        Capability.KUBECTL,
        Capability.S5CMD_FRONTEND,
    } <= required_capabilities(config)


def test_images_reject_invalid_overrides():
    with pytest.raises(ValueError):
        resolve_images("bad tag", {})
    with pytest.raises(ValueError):
        resolve_images(None, {"blocksci": "bad image"})


def test_manifest_redacts_secrets():
    assert (
        redact({"configuration": {"artifacts": {"credentials_file": "/private/credentials"}}})["configuration"][
            "artifacts"
        ]["credentials_file"]
        == "<redacted>"
    )


def test_run_id_is_safe_and_bounded(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"name": "Příliš žluťoučký " * 20}))
    value = run_id_for(load_configuration(["emulate", "--scenario", str(scenario)]))
    assert validate_run_id(value) == value
    assert len(value) <= 63
