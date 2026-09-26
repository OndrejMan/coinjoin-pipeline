"""Regression checks at the host CLI / wrapper boundary, without backends."""

from pathlib import Path

import pytest

from coinjoin_pipeline.commands import option_value
from coinjoin_pipeline.runs import manifest_target, run_id_for


@pytest.mark.parametrize(
    "arguments",
    [
        ["--run-id", "old-run", "--run-id", "new-run"],
        ["--run-id=old-run", "--run-id=new-run"],
        ["--run-id=old-run", "--run-id", "new-run"],
    ],
)
def test_host_run_id_matches_argparse_last_value(arguments: list[str]) -> None:
    assert option_value(arguments, "--run-id") == "new-run"
    assert run_id_for(arguments) == "new-run"
    assert manifest_target("emulate", arguments, Path("/runs")) == (
        Path("/runs/new-run/research_manifest.json")
    )
