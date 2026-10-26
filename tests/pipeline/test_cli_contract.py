import pytest

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.cli import main
from coinjoin_pipeline.context import resolve_configuration


def test_top_level_help_exposes_actions_and_configuration_options(capsys, monkeypatch):
    def no_execution(*_args, **_kwargs):
        pytest.fail("help must not prepare or execute a run")

    monkeypatch.setattr("coinjoin_pipeline.cli.RunContext.prepare", no_execution)
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "cjp run CONFIG.yaml" in output
    assert "--analysis-program" in output
    assert "mainnet-sync" in output
    assert "pbs-from-s3" in output
    assert "download-report" in output
    assert "--dry-run" in output


@pytest.mark.parametrize(
    "flags",
    [
        ("--blocksci-script", "--blocksciScript"),
        ("--blocksciScript", "--blocksci-script"),
    ],
)
def test_option_aliases_follow_cli_order(flags):
    config = load_configuration(["analyze", "--run-dir", "run", flags[0], "old.py", flags[1], "new.py"])
    assert config.blocksci.script == "new.py"


@pytest.mark.parametrize("action", ["analyze", "export", "full-run"])
def test_analysis_actions_reject_unknown_coinjoin_type(action):
    with pytest.raises(SystemExit):
        load_configuration([action, "--coinjoin-type", "unknown"])


def test_repeated_values_are_resolved_only_once():
    config = load_configuration(
        [
            "emulate",
            "--engine",
            "wasabi",
            "--engine=joinmarket",
            "--run-id=old",
            "--run-id",
            "new",
        ]
    )
    assert config.engine == "joinmarket"
    assert config.run_id == "new"
    assert resolve_configuration(config).coinjoin_type == "joinmarket"
