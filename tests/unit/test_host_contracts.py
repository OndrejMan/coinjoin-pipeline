"""Regression checks at the host CLI / wrapper boundary, without backends."""

from pathlib import Path
from unittest import mock

import pytest

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.cli import main
from coinjoin_pipeline.runs import run_id_for


@pytest.mark.parametrize(
    "arguments",
    [
        ["--run-id", "old-run", "--run-id", "new-run"],
        ["--run-id=old-run", "--run-id=new-run"],
        ["--run-id=old-run", "--run-id", "new-run"],
    ],
)
def test_host_run_id_matches_argparse_last_value(arguments: list[str]) -> None:
    config = load_configuration(arguments)
    assert config.run_id == "new-run"
    assert run_id_for(config) == "new-run"


def test_local_build_preserves_explicit_and_environment_image_overrides(
    tmp_path: Path,
) -> None:
    with (
        mock.patch.dict("os.environ", {"BLOCKSCI_IMAGE": "blocksci:env"}, clear=True),
        mock.patch("coinjoin_pipeline.cli.doctor_check", return_value=[]),
        mock.patch("coinjoin_pipeline.cli.execute") as execute,
    ):
        assert (
            main(
                [
                    "--runs-root",
                    str(tmp_path),
                    "emulate",
                    "--engine",
                    "wasabi",
                    "--run-id",
                    "custom-images",
                    "--local-build",
                    "--emulator-image",
                    "emulator:custom",
                ]
            )
            == 0
        )
        env = execute.call_args.args[0].environment
    assert env["COINJOIN_EMULATOR_IMAGE"] == "emulator:custom"
    assert env["BLOCKSCI_IMAGE"] == "blocksci:env"
    assert env["COINJOIN_ANALYSIS_IMAGE"] == "coinjoin-analysis:local"


def test_local_build_rejects_invalid_image_overrides() -> None:
    with mock.patch("coinjoin_pipeline.cli.doctor_check", return_value=[]):
        assert (
            main(
                [
                    "emulate",
                    "--engine",
                    "wasabi",
                    "--local-build",
                    "--emulator-image",
                    "not an image",
                    "--dry-run",
                ]
            )
            == 2
        )
