"""Public CLI coverage and data isolation without launching analyzers or accessing S3/PBS."""

import ast
import json
import os
from pathlib import Path
import shlex
from unittest.mock import Mock

import pytest

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.configuration import ConfigurationError, PipelineConfiguration
from coinjoin_pipeline.execution import coinjoin_tools
from coinjoin_pipeline.execution.pbs.templates_s3 import render_coinjoin_tools_s3_pbs
from coinjoin_pipeline.execution.s3_submission import S3StageRunner, s3_full_run_plan_from_args
from coinjoin_pipeline.storage.s3 import S3Target


def config(options, **overrides):
    options = {**options}
    if "data_mode" not in options and options.get("program") in coinjoin_tools.MAINNET_PROGRAMS:
        options["data_mode"] = "mainnet"
    return PipelineConfiguration.from_mapping(
        {
            "action": "pbs-from-s3",
            "run_id": "r1",
            "stages": {"analysis": True},
            "artifacts": {
                "backend": "s3",
                "uri": "s3://bucket/runs",
                "endpoint_url": "https://s3.example",
                "credentials_file": "/tmp/credentials",
                "profile": "test",
            },
            "coinjoin_analysis": options,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "program,actions",
    [
        (program, actions)
        for program, actions in coinjoin_tools.PROGRAM_ACTIONS.items()
        if program not in coinjoin_tools.RESEARCH_PROGRAMS | coinjoin_tools.WORKFLOW_PROGRAMS
    ],
)
def test_catalog_matches_the_analyzers_public_argparse_choices(program, actions):
    package = "helpers" if program == "perf_processing" else "cj_process"
    source = Path(__file__).resolve().parents[3] / f"coinjoin-analysis/src/{package}" / f"{program}.py"
    if not source.is_file():
        pytest.skip("Sibling analyzer source is not installed")
    tree = ast.parse(source.read_text())
    choices = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and any(isinstance(arg, ast.Constant) and arg.value == "--action" for arg in node.args)
        ):
            choices = next(ast.literal_eval(item.value) for item in node.keywords if item.arg == "choices")
    assert tuple(choices) == actions


def test_catalog_covers_all_existing_commands_with_configurable_cli_inputs():
    source = Path(__file__).resolve().parents[3] / "coinjoin-analysis/src/cj_process"
    if not source.is_dir():
        pytest.skip("Sibling analyzer source is not installed")
    commands = set()
    for path in [*source.glob("*.py"), source.parent / "helpers/perf_processing.py"]:
        tree = ast.parse(path.read_text())
        if not any(
            isinstance(node, ast.Compare)
            and any(isinstance(item, ast.Constant) and item.value == "__main__" for item in node.comparators)
            for node in ast.walk(tree)
        ):
            continue
        parser = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "ArgumentParser"
            for node in ast.walk(tree)
        )
        positional = any(
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "sys"
            and node.value.attr == "argv"
            for node in ast.walk(tree)
        )
        if parser or positional:
            commands.add(path.stem)
    assert commands == set(coinjoin_tools.PROGRAM_ACTIONS) - coinjoin_tools.RESEARCH_PROGRAMS - coinjoin_tools.WORKFLOW_PROGRAMS


@pytest.mark.parametrize("program,actions", coinjoin_tools.PROGRAM_ACTIONS.items())
def test_every_public_operation_can_be_selected_without_report_dependencies(program, actions):
    for action in actions or (None,):
        options = {"program": program, "actions": [action] if action else [], "input_uri": "s3://bucket/data"}
        if program == "scan_results_plot":
            options["filename"] = "wasabi2"
        if program == "perf_processing":
            options["target_paths"] = ["memlog.csv"]
        if program == "ww2_analyze_client" and action != "process_emulations":
            options["data_mode"] = "mainnet"
        if program == "analyze_tx_size":
            options["research"] = {"tx_hex": "00", "input_indices": [0]}
        if program == "update_txids_with_wabisator":
            options["research"] = {"allow_network": True}
        model = config(options)
        model.validate()
        assert [stage.name for stage in s3_full_run_plan_from_args(model).scheduled()] == ["coinjoin-analysis"]


def test_cli_accepts_multiple_actions_targets_and_switches_as_values():
    model = load_configuration(
        [
            "coinjoin-analysis",
            "--run-dir",
            "r1",
            "--analysis-program",
            "parse_cj_logs",
            "--analysis-operation",
            "analyze_only",
            "--analysis-target-path",
            "set one",
            "--analysis-target-path",
            "set two",
            "--analysis-load-config",
            "config.json",
            "--analysis-env-vars",
            "DEBUG=False;NAME='$(touch /tmp/never)'",
        ]
    )
    model.validate()
    argv = coinjoin_tools.command_argv(model.coinjoin_analysis, "r1")
    assert argv.count("--target-path") == 2
    assert "/work/input/set one" in argv
    assert argv[-1] == "DEBUG=False;NAME='$(touch /tmp/never)'"
    assert shlex.split(coinjoin_tools.command(model.coinjoin_analysis, "r1")) == argv


@pytest.mark.parametrize(
    "options,match",
    [
        ({"program": "parse_cj_logs", "actions": ["plot_coinjoins"]}, "invalid operation"),
        ({"program": "parse_cj_logs", "actions": ["collect_docker", "analyze_only"]}, "exactly one"),
        ({"program": "parse_cj_logs", "actions": ["analyze_only"], "target_paths": ["../outside"]}, "relative"),
        ({"program": "parse_dumplings", "actions": ["plot_coinjoins"]}, "own input dataset"),
        ({"program": "ww2_analyze_client", "actions": ["compute_num_predict_matrix"]}, "own input dataset"),
        ({"program": "scan_results_plot", "filename": "wasabi2.json", "input_uri": "s3://bucket/data"}, "filename"),
        ({"program": "parse_cj_logs", "actions": ["analyze_only"], "load_config": "/host/config"}, "relative"),
        ({"program": "coinjoin_statistics", "actions": [], "input_uri": "s3://bucket/data"}, "at least one"),
        ({"program": "coinjoin_statistics", "actions": ["wasabi2"]}, "own input dataset"),
        (
            {"program": "coinjoin_statistics", "actions": ["wasabi2"], "data_mode": "emulation", "input_uri": "s3://b/d"},
            "mainnet operation",
        ),
        (
            {"program": "coinjoin_statistics", "actions": ["wasabi2"], "env_vars": "A=1", "input_uri": "s3://b/d"},
            "only its protocols",
        ),
        (
            {"program": "coinjoin_statistics", "actions": ["wasabi2", "wasabi2"], "input_uri": "s3://b/d"},
            "each protocol once",
        ),
    ],
)
def test_invalid_combinations_fail_before_execution(options, match):
    with pytest.raises(ConfigurationError, match=match):
        config(options).validate()


def test_supplemental_operations_cannot_replace_the_report_baseline():
    with pytest.raises(ConfigurationError, match="run alone"):
        config(
            {"program": "parse_cj_logs", "actions": ["analyze_only"]}, stages={"analysis": True, "blocksci": True}
        ).validate()


def test_s3_runner_dispatches_the_selected_program_and_retains_all_outputs(monkeypatch):
    model = config(
        {
            "program": "parse_dumplings",
            "actions": ["plot_coinjoins", "detect_coordinators"],
            "input_uri": "s3://bucket/snapshots",
            "env_vars": 'NAME="a\'b"',
        }
    )
    target = S3Target.from_args(model)
    runner = S3StageRunner(model, target, s3_full_run_plan_from_args(model), [])
    submitted = Mock()
    monkeypatch.setattr(runner, "_submit", submitted)
    runner.submit_all()
    script = submitted.call_args.args[1]
    assert "cj_process.parse_dumplings" in script
    assert "--action plot_coinjoins --action detect_coordinators" in script
    assert "s3://bucket/snapshots/*" in script
    assert "coinjoin-analysis-tools_data/parse_dumplings/$RESULT_ID/" in script
    assert 'tee "$OUTPUT/analysis.log"' in script
    assert "stage_finalize()" in script
    assert 'require_file "$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json"' not in script


def test_local_uses_a_copy_logs_results_and_keeps_the_baseline(monkeypatch, tmp_path):
    run = tmp_path / "r1"
    baseline = run / "coinjoin-analysis_data"
    baseline.mkdir(parents=True)
    (baseline / "coinjoin_tx_info.json").write_text('{"original": true}')
    data = run / "coinjoin_emulator_data/data/btc-node"
    data.mkdir(parents=True)
    (data / "block_1.json").write_text("{}")
    model = config(
        {"program": "parse_cj_logs", "actions": ["analyze_only"]}, action="coinjoin-analysis", run_dir="r1", stages={}
    )

    def execute(argv, **kwargs):
        volume = argv[argv.index("--volume") + 1].removesuffix(":/work:rw")
        work = Path(volume)
        copied = work / "input/r1/coinjoin_tx_info.json"
        assert copied.read_text() == '{"original": true}'
        copied.write_text('{"changed": true}')
        assert (work / "input/r1/data/btc-node/block_1.json").is_file()
        kwargs["stdout"].write("finished\n")

    monkeypatch.setattr(coinjoin_tools.subprocess, "run", execute)
    first = coinjoin_tools.run_local(model, run)
    second = coinjoin_tools.run_local(model, run)
    assert first != second
    assert (first / "analysis.log").read_text() == "finished\n"
    assert json.loads((first / "invocation.json").read_text())["argv"][2] == "cj_process.parse_cj_logs"
    assert (baseline / "coinjoin_tx_info.json").read_text() == '{"original": true}'


def test_local_dry_run_does_not_copy_inputs_or_start_a_container(monkeypatch, tmp_path):
    model = config(
        {"program": "parse_cj_logs", "actions": ["analyze_only"]},
        action="coinjoin-analysis",
        run_dir="r1",
        stages={},
        dry_run=True,
    )
    execute = Mock()
    monkeypatch.setattr(coinjoin_tools.subprocess, "run", execute)
    output = coinjoin_tools.run_local(model, tmp_path / "r1")
    assert not output.exists()
    execute.assert_not_called()


def test_rendered_shell_handles_quoted_values(tmp_path):
    import subprocess

    model = config(
        {"program": "parse_cj_logs", "actions": ["analyze_only"], "env_vars": 'NAME="a\'b $(never)";DEBUG=False'}
    )
    script = render_coinjoin_tools_s3_pbs(
        S3Target.from_args(model), "docker://test/image:latest", model.coinjoin_analysis
    )
    path = tmp_path / "job.sh"
    path.write_text(script)
    subprocess.run(["bash", "-n", str(path)], check=True)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_pbs_shell_publishes_outputs_and_failure_logs_without_running_services(tmp_path, exit_code):
    """Exercise the generated shell with fake executables, including the exit trap."""
    import subprocess

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    uploaded = tmp_path / "uploaded"
    uploaded.mkdir()
    credentials = tmp_path / "credentials"
    credentials.write_text("local mock, not a real credential")
    # Record upload contents before the real stage trap clears only this test's scratch.
    s5cmd = mock_bin / "s5cmd"
    s5cmd.write_text("""#!/usr/bin/env python3
import os, pathlib, shutil, sys
args = sys.argv[7:]
root = pathlib.Path(os.environ['TEST_UPLOAD'])
if args[0] == 'sync':
    source, destination = args[1:]
    if source.startswith('s3://'):
        target = pathlib.Path(destination)
        target.mkdir(parents=True, exist_ok=True)
        (target / 'sample.json').write_text('{}')
    else:
        shutil.copytree(source, root / 'result', dirs_exist_ok=True)
elif args[0] == 'cp':
    shutil.copy(args[1], root / pathlib.Path(args[1]).name)
""")
    s5cmd.chmod(0o755)
    singularity = mock_bin / "singularity"
    singularity.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
output = pathlib.Path(args[args.index('--bind') + 1].removesuffix(':/work:rw'))
(output / 'plot.png').write_text('fake plot')
json.dump(args, open(output / 'container-arguments.json', 'w'))
print('analyzer output')
sys.exit(int(os.environ['TEST_ANALYZER_EXIT']))
""")
    singularity.chmod(0o755)
    model = config(
        {
            "program": "parse_dumplings",
            "actions": ["plot_coinjoins"],
            "input_uri": "s3://bucket/dataset",
            "env_vars": 'NAME="a\'b $(never)"',
        },
        artifacts={
            "backend": "s3",
            "uri": "s3://bucket/runs",
            "endpoint_url": "https://s3.example",
            "credentials_file": str(credentials),
            "profile": "test",
        },
    )
    job = tmp_path / "job.sh"
    job.write_text(
        render_coinjoin_tools_s3_pbs(S3Target.from_args(model), "docker://test/image:latest", model.coinjoin_analysis)
    )
    result = subprocess.run(
        ["bash", str(job)],
        env={
            **os.environ,
            "PATH": f"{mock_bin}:{os.environ['PATH']}",
            "SCRATCHDIR": str(scratch),
            "TEST_UPLOAD": str(uploaded),
            "TEST_ANALYZER_EXIT": str(exit_code),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == exit_code, result.stderr
    assert (uploaded / "result/plot.png").is_file()
    assert (uploaded / "result/analysis.log").read_text() == "analyzer output\n"
    arguments = json.loads((uploaded / "result/container-arguments.json").read_text())
    assert shlex.split(arguments[-1].removeprefix("cd /work && ")) == coinjoin_tools.command_argv(
        model.coinjoin_analysis, "r1"
    )
    assert (uploaded / ("coinjoin-analysis.done" if exit_code == 0 else "coinjoin-analysis.failed")).is_file()
