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


def test_help_documents_every_option_with_its_yaml_key(capsys):
    from typing import get_args

    from coinjoin_pipeline.arguments import ACTION_SUMMARIES, HELP, SECTIONS, build_parser
    from coinjoin_pipeline.configuration import PipelineAction, option_specs

    paths = {path for path, _, _ in option_specs()}
    assert set(HELP) <= paths, "help text for an option that no longer exists"
    assert {path.split(".", 1)[0] if "." in path else "" for path in paths} <= set(SECTIONS)
    assert set(ACTION_SUMMARIES) == set(get_args(PipelineAction))
    build_parser().print_help()
    output = " ".join(capsys.readouterr().out.split())
    for path in paths:
        assert f"YAML: {path}" in output
    assert "exit codes:" in output


@pytest.mark.parametrize("argv", [["doctor", "--help"], ["pull", "-h"], ["run", "--help"]])
def test_help_of_commands_without_their_own_parser(argv, capsys, monkeypatch):
    monkeypatch.setattr("coinjoin_pipeline.cli.RunContext.prepare", lambda *_: pytest.fail("help must not run"))
    assert main(argv) == 0
    assert "usage: cjp" in capsys.readouterr().out


def test_unknown_action_lists_the_valid_ones(capsys):
    assert main(["builder", "--dry-run"]) == 2
    error = capsys.readouterr().err
    assert "unknown action 'builder'" in error
    assert "full-run" in error and "download-report" in error


def test_yaml_and_command_line_share_action_aliases(tmp_path):
    experiment = tmp_path / "experiment.yaml"
    experiment.write_text("action: coinjoin\nrun_dir: run-a\n")
    assert load_configuration(["run", str(experiment)]).action == "coinjoin-analysis"
    assert load_configuration(["coinjoin", "--run-dir", "run-a"]).action == "coinjoin-analysis"
    assert load_configuration(["external-analyze", "--run-id", "x"]).action == "external analyze"


def test_value_errors_name_the_flag_as_well_as_the_yaml_key():
    from coinjoin_pipeline.configuration import ConfigurationError, PipelineConfiguration

    with pytest.raises(ConfigurationError, match=r"configuration\.min_input_count \(--min-input-count\) must be"):
        PipelineConfiguration.from_mapping({"min_input_count": 0})
    with pytest.raises(ConfigurationError, match=r"configuration\.pbs\.blocksci\.ncpus \(--pbs-blocksci-ncpus\)"):
        PipelineConfiguration.from_mapping({"pbs": {"blocksci": {"ncpus": "8"}}})


def test_dry_run_prints_the_resolved_run_and_images(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("coinjoin_pipeline.cli.execute", lambda _context: None)
    argv = ["full-run", "--runs-root", str(tmp_path), "--run-id", "run-a", "--version", "v9", "--dry-run"]
    assert main(argv) == 0
    output = capsys.readouterr().out
    assert f"[dry-run] run directory: {tmp_path / 'run-a'}" in output
    assert "[dry-run] image blocksci: ghcr.io/ondrejman/blocksci-complete:v9" in output


def test_every_preflight_error_gets_its_own_line(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("coinjoin_pipeline.cli.execute", lambda _context: pytest.fail("must not execute"))
    monkeypatch.setattr("coinjoin_pipeline.cli.doctor_check", lambda *_args, **_kwargs: ["first", "second"])
    assert main(["emulate", "--runs-root", str(tmp_path), "--run-id", "run-a"]) == 2
    assert capsys.readouterr().err.splitlines() == ["ERROR: first", "ERROR: second"]
