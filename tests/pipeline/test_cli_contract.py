import pytest

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.configuration import ConfigurationError, PipelineConfiguration
from coinjoin_pipeline.cli import main
from coinjoin_pipeline.context import resolve_configuration


def experiment(tmp_path, text, name="experiment.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_top_level_help_exposes_actions_yaml_keys_and_overrides(capsys, monkeypatch):
    def no_execution(*_args, **_kwargs):
        pytest.fail("help must not prepare or execute a run")

    monkeypatch.setattr("coinjoin_pipeline.cli.RunContext.prepare", no_execution)
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "cjp run CONFIG.yaml" in output
    assert "coinjoin_analysis.program" in output
    assert "mainnet-sync" in output
    assert "pbs-from-s3" in output
    assert "download-report" in output
    assert "--dry-run" in output


def test_only_launch_values_are_command_line_options(tmp_path):
    path = experiment(tmp_path, "action: emulate\nengine: wasabi\n")
    with pytest.raises(SystemExit):
        load_configuration(["run", path, "--engine", "joinmarket"])
    config = load_configuration(["run", path, "--run-id", "old", "--run-id", "new", "--dry-run"])
    assert (config.run_id, config.dry_run) == ("new", True)


@pytest.mark.parametrize("action", ["analyze", "export", "full-run"])
def test_analysis_actions_reject_unknown_coinjoin_type(action):
    with pytest.raises(ConfigurationError):
        PipelineConfiguration.from_paths({"action": action, "coinjoin_type": "unknown"})


def test_an_override_replaces_the_file_value(tmp_path):
    path = experiment(tmp_path, "action: emulate\nengine: joinmarket\nrun_id: old\n")
    config = load_configuration(["run", path, "--run-id", "new"])
    assert config.run_id == "new"
    assert resolve_configuration(config).coinjoin_type == "joinmarket"


def test_set_replaces_yaml_keys_with_their_types(tmp_path):
    path = experiment(tmp_path, "action: full-run\nengine: wasabi\npbs:\n  ncpus: 2\n")
    config = load_configuration(
        [
            "run",
            path,
            "--set",
            "pbs.ncpus=4",
            "--set",
            "pbs.walltime=00:30:00",
            "--set",
            "stages.blocksci=true",
            "--set",
            "coinjoin_analysis.actions=detect_coordinators",
            "--set",
            'coinjoin_analysis.research.input_indices=[0, 2]',
            "--set",
            'kubernetes.planned_quota={"cpu": 2}',
        ]
    )
    assert config.pbs.ncpus == 4
    assert config.pbs.walltime == "00:30:00"
    assert config.stages.blocksci is True
    assert config.coinjoin_analysis.actions == ("detect_coordinators",)
    assert config.coinjoin_analysis.research.input_indices == (0, 2)
    assert config.kubernetes.planned_quota == '{"cpu": 2}'


def test_later_set_wins_and_named_overrides_win_over_set(tmp_path):
    path = experiment(tmp_path, "action: emulate\nengine: wasabi\n")
    config = load_configuration(["run", path, "--set", "engine=joinmarket", "--set", "engine=wasabi"])
    assert config.engine == "wasabi"
    with pytest.raises(ConfigurationError, match="use --run-id"):
        load_configuration(["run", path, "--set", "run_id=x"])


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("pbs.ncpus", "expects KEY=VALUE"),
        ("=4", "expects KEY=VALUE"),
        ("pbs.nope=1", "unknown YAML key 'pbs.nope'"),
        ("pbs=1", "unknown YAML key 'pbs'"),
        ("pbs.ncpus=four", "pbs.ncpus must be an integer"),
        ("engine=bitcoin", "engine must be one of"),
    ],
)
def test_set_rejects_malformed_and_unknown_keys(tmp_path, setting, message):
    path = experiment(tmp_path, "action: emulate\nengine: wasabi\n")
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(["run", path, "--set", setting])


def test_utilities_do_not_take_set(capsys):
    assert main(["doctor", "--set", "pbs.ncpus=4"]) != 0
    assert "--set" in capsys.readouterr().err


def test_help_documents_every_yaml_key(capsys):
    from typing import get_args

    from coinjoin_pipeline.arguments import ACTION_SUMMARIES, HELP, OVERRIDES, SECTIONS, build_parser
    from coinjoin_pipeline.configuration import PipelineAction, option_specs

    paths = {path for path, _ in option_specs()}
    assert set(HELP) <= paths, "help text for a key that no longer exists"
    assert set(OVERRIDES) <= paths
    assert {path.split(".", 1)[0] if "." in path else "" for path in paths} <= set(SECTIONS)
    assert set(ACTION_SUMMARIES) == set(get_args(PipelineAction))
    build_parser().print_help()
    output = capsys.readouterr().out.split()
    for path in paths:
        assert path in output
    assert "exit" in output


@pytest.mark.parametrize("argv", [["doctor", "--help"], ["pull", "-h"], ["run", "--help"]])
def test_help_of_commands_without_their_own_parser(argv, capsys, monkeypatch):
    monkeypatch.setattr("coinjoin_pipeline.cli.RunContext.prepare", lambda *_: pytest.fail("help must not run"))
    assert main(argv) == 0
    assert "usage: cjp" in capsys.readouterr().out


def test_unknown_command_lists_the_valid_ones(capsys):
    assert main(["builder", "--dry-run"]) == 2
    error = capsys.readouterr().err
    assert "unknown command 'builder'" in error
    assert "cjp run CONFIG.yaml" in error and "download-report" in error


def test_an_action_word_points_to_the_yaml_file(capsys):
    assert main(["analyze", "--run-dir", "run-a"]) == 2
    assert "put `action: analyze` in a file" in capsys.readouterr().err


def test_yaml_takes_one_spelling_of_each_action(tmp_path):
    path = experiment(tmp_path, "action: coinjoin\nrun_dir: run-a\n")
    with pytest.raises(ConfigurationError):
        load_configuration(["run", path])
    path = experiment(tmp_path, "action: external-analyze\nrun_id: x\n")
    assert load_configuration(["run", path]).action == "external-analyze"


def test_value_errors_name_the_yaml_key():
    with pytest.raises(ConfigurationError, match=r"configuration\.min_input_count must be"):
        PipelineConfiguration.from_mapping({"min_input_count": 0})
    with pytest.raises(ConfigurationError, match=r"configuration\.pbs\.blocksci\.ncpus must be"):
        PipelineConfiguration.from_mapping({"pbs": {"blocksci": {"ncpus": "8"}}})


def test_dry_run_prints_the_resolved_run_and_images(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("coinjoin_pipeline.cli.execute", lambda _context: None)
    path = experiment(tmp_path, "action: full-run\nscenario: scenarios/overactive-local.json\n")
    argv = ["run", path, "--runs-root", str(tmp_path), "--run-id", "run-a", "--version", "v9", "--dry-run"]
    assert main(argv) == 0
    output = capsys.readouterr().out
    assert f"[dry-run] run directory: {tmp_path / 'run-a'}" in output
    assert "[dry-run] image blocksci: ghcr.io/ondrejman/blocksci-complete:v9" in output


@pytest.mark.parametrize("text", ["action: emulate\n", "action: full-run\n", "action: full-run\nengine: wasabi\n"])
def test_a_run_requires_an_explicit_scenario(tmp_path, capsys, monkeypatch, text):
    monkeypatch.setattr("coinjoin_pipeline.cli.execute", lambda _context: pytest.fail("must not execute"))
    assert main(["run", experiment(tmp_path, text), "--runs-root", str(tmp_path), "--dry-run"]) == 2
    assert "requires scenario; there is no default scenario" in capsys.readouterr().err


def test_doctor_needs_no_scenario(tmp_path, monkeypatch):
    monkeypatch.setattr("coinjoin_pipeline.cli.doctor_check", lambda *_args, **_kwargs: [])
    assert main(["doctor", "--runs-root", str(tmp_path)]) == 0


def test_every_preflight_error_gets_its_own_line(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("coinjoin_pipeline.cli.execute", lambda _context: pytest.fail("must not execute"))
    monkeypatch.setattr("coinjoin_pipeline.cli.doctor_check", lambda *_args, **_kwargs: ["first", "second"])
    path = experiment(tmp_path, "action: emulate\nscenario: scenarios/overactive-local.json\n")
    assert main(["run", path, "--runs-root", str(tmp_path), "--run-id", "run-a"]) == 2
    assert capsys.readouterr().err.splitlines() == ["ERROR: first", "ERROR: second"]


def test_every_key_has_a_description():
    from coinjoin_pipeline.arguments import option_help
    from coinjoin_pipeline.configuration import option_specs

    undescribed = [path for path, item in option_specs() if not option_help(path, item).split(" [default:")[0]]
    assert undescribed == []


def test_the_scenario_catalogue_is_one_variable_read_when_needed(tmp_path, monkeypatch):
    from coinjoin_pipeline.execution.containers import compose_env
    import shutil

    from coinjoin_pipeline.execution.scenarios import packaged_scenarios, resolve_scenario
    from coinjoin_pipeline.paths import SCENARIOS_ROOT

    catalogue = tmp_path / "catalogue"
    catalogue.mkdir()
    shutil.copyfile(SCENARIOS_ROOT / "overactive-local.json", catalogue / "tiny.json")
    monkeypatch.setenv("SCENARIOS_DIR", str(catalogue))
    monkeypatch.setenv("EMULATION_LOGS_DIR", str(tmp_path / "runs"))
    assert resolve_scenario("tiny") == catalogue / "tiny.json"
    assert [entry["path"] for entry in packaged_scenarios()] == [str(catalogue / "tiny.json")]
    assert compose_env(include_scenario=False, create_directories=False)["SCENARIOS_DIR"] == str(catalogue)
