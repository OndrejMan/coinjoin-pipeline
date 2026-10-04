import json
from pathlib import Path
from unittest import mock

import pytest
from exporters import cli as report_cli
from exporters import worker
from exporters.analysis_artifact import detector_parameters

from coinjoin_pipeline import cli
from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.context import RunContext, resolve_configuration
from coinjoin_pipeline.execution import containers, research


def report_inputs(run_dir: Path) -> None:
    baseline = run_dir / "coinjoin-analysis_data/coinjoin_tx_info.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text('{"coinjoins": {}}')
    scenario = run_dir / "coinjoin_emulator_data/scenario.json"
    scenario.parent.mkdir()
    scenario.write_text('{"name": "fixture"}')
    args = worker.parse_args(["report", "--run-dir", str(run_dir)])
    artifact = run_dir / "blocksci-analysis_data/blocksci_analysis.json"
    artifact.parent.mkdir()
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "mode": "emulator",
                "run_id": run_dir.name,
                "parameters": detector_parameters(args),
                "first_wasabi2_block": 0,
                "records": {},
                "skipped_txids": [],
                "integration_diagnostics": {"status": "ok"},
                "predicted_address_clusters": {},
                "cluster_export_error": None,
            }
        )
    )


def test_worker_really_assembles_report_from_path_object(tmp_path):
    run = tmp_path / "run-a"
    report_inputs(run)
    assert worker.main(["report", "--run-dir", str(run)]) == 0
    assert (run / "coinjoinPipeline_data/unified_report.json").is_file()
    assert (run / "coinjoinPipeline_data/unified_report.md").is_file()


def test_report_accepts_relative_runs_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = Path("runs/run-a")
    report_inputs(run)
    assert report_cli.main(["--runs-root", "runs", "--run-dir", "run-a"]) == 0
    assert (run / "coinjoinPipeline_data/unified_report.json").is_file()


def test_export_needs_only_analytic_artifacts_and_report_image(tmp_path):
    run = tmp_path / "run-a"
    report_inputs(run)
    context = RunContext.prepare(
        load_configuration(
            [
                "export",
                "--runs-root",
                str(tmp_path),
                "--run-dir",
                "run-a",
                "--unified-report-image",
                "python:report-test",
            ]
        ),
        "test",
    )
    with (
        context.activate(),
        mock.patch.object(containers, "inspect_image_provenance", return_value=(None, None)),
        mock.patch.object(containers, "run_command") as execute,
    ):
        containers.run_export_only(context.config)
    command = execute.call_args.args[0]
    assert "python:report-test" in command
    assert command[command.index("python:report-test") + 1 :][:3] == ["python3", "/mnt/exporters/worker.py", "report"]
    assert "compose" not in command


def test_runs_validate_preserves_explicit_image_override(tmp_path):
    with mock.patch.object(research, "validate_existing_run") as validate:
        assert (
            cli.main(
                [
                    "runs",
                    "validate",
                    "--run-dir",
                    "run-a",
                    "--runs-root",
                    str(tmp_path),
                    "--blocksci-image",
                    "blocksci:chosen",
                ]
            )
            == 0
        )
    assert validate.call_args.args[0].blocksci_image == "blocksci:chosen"


def test_utility_help_belongs_to_selected_command(capsys):
    assert cli.main(["watch", "--help"]) == 0
    assert "--pbs-only" in capsys.readouterr().out


def test_external_requires_run_identity_before_execution():
    config = load_configuration(
        ["external", "analyze", "--bitcoin-datadir", "/tmp/btc", "--baseline", "/tmp/baseline.json"]
    )
    with pytest.raises(ValueError, match="run.id"):
        resolve_configuration(config)


@pytest.mark.parametrize(
    "argv",
    [
        ["full-run", "--run-dir", "old-run"],
        ["export", "--all-runs"],
        ["analyze", "--parallel", "--run-dir", "run-a"],
    ],
)
def test_unsupported_selectors_cannot_be_silently_ignored(argv):
    with pytest.raises(ValueError):
        resolve_configuration(load_configuration(argv))


def test_worker_accepts_relative_run_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = Path("runs/run-a")
    report_inputs(run)
    assert worker.main(["report", "--run-dir", str(run)]) == 0
    assert (run / "coinjoinPipeline_data/unified_report.json").is_file()


@pytest.mark.parametrize("text", ["false", "0", "[]"])
def test_invalid_yaml_document_never_becomes_default_experiment(tmp_path, text):
    path = tmp_path / "experiment.yaml"
    path.write_text(text)
    with pytest.raises(ValueError, match="mapping"):
        load_configuration(["run", str(path)])


@pytest.mark.parametrize(
    "entry", ["blocksci_export/analysis.py", "markdown_report.py", "unified_report.py", "worker.py"]
)
def test_analysis_script_is_standalone_without_host_package(tmp_path, entry):
    import os
    import shutil
    import subprocess
    import sys

    from coinjoin_pipeline.paths import EXPORTERS_ROOT

    target = tmp_path / "exporters"
    shutil.copytree(EXPORTERS_ROOT, target, ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(
        [sys.executable, "-S", str(target / entry), "--help"],
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_shared_report_budget_matches_wait_budget_and_selected_image(tmp_path):
    from coinjoin_pipeline.execution import shared_storage_pbs
    from coinjoin_pipeline.execution.pbs_settings import stage_pbs_walltime

    config = RunContext.prepare(
        load_configuration(
            [
                "full-run",
                "--blocksciPbs",
                "--pbs-bitcoin-datadir",
                str(tmp_path / "btc"),
                "--pbs-unified-report-walltime",
                "03:00:00",
                "--pbs-unified-report-ncpus",
                "3",
                "--unified-report-image",
                "python:report-test",
                "--runs-root",
                str(tmp_path),
                "--dry-run",
            ]
        ),
        "test",
    ).config
    run = tmp_path / "run-a"
    with (
        mock.patch.object(containers, "inspect_image_provenance", return_value=(None, None)),
        mock.patch.object(shared_storage_pbs, "submit_blocksci_pbs") as submit,
    ):
        shared_storage_pbs.run_blocksci_export_stage(config, run, wait=False)
    values = submit.call_args.kwargs
    assert values["walltime"] == stage_pbs_walltime(config, "report") == "03:00:00"
    assert values["ncpus"] == 3
    assert values["image"] == "docker://python:report-test"
    assert not run.exists()


def test_shared_storage_dry_run_never_writes_job_files(tmp_path):
    from coinjoin_pipeline.execution.pbs import submission_local as submission

    with (
        mock.patch.object(submission, "require_storage_path"),
        mock.patch.object(submission, "require_existing_path"),
        mock.patch.object(submission, "require_bitcoin_datadir"),
        mock.patch.object(submission, "require_qsub") as qsub,
    ):
        submission.submit_blocksci_pbs(
            tmp_path / "run-a", tmp_path, tmp_path / "btc", tmp_path / "exporters", "image:test", "true", dry_run=True
        )
    qsub.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_report_preserves_producer_image_instead_of_current_host_image(tmp_path):
    run = tmp_path / "run-a"
    report_inputs(run)
    path = run / "blocksci-analysis_data/blocksci_analysis.json"
    data = json.loads(path.read_text())
    data["image_provenance"] = {"blocksci": {"reference": "blocksci:producer", "repo_digest": "sha256:producer"}}
    path.write_text(json.dumps(data))
    assert (
        report_cli.main(
            ["--run-dir", str(run), "--blocksci-image", "blocksci:other", "--blocksci-image-digest", "sha256:other"]
        )
        == 0
    )
    report = json.loads((run / "coinjoinPipeline_data/unified_report.json").read_text())
    assert report["run_manifest"]["images"]["blocksci"] == "blocksci:producer"
    assert report["run_manifest"]["image_digests"]["blocksci"] == "sha256:producer"


def test_report_keeps_its_uploader_image_when_the_producer_recorded_none(tmp_path):
    run = tmp_path / "run-a"
    report_inputs(run)
    path = run / "blocksci-analysis_data/blocksci_analysis.json"
    data = json.loads(path.read_text())
    data["image_provenance"] = {"uploader": {"reference": None, "image_id": None, "repo_digest": None}}
    path.write_text(json.dumps(data))
    assert report_cli.main(["--run-dir", str(run), "--uploader-image", "uploader@sha256:" + "a" * 64]) == 0
    report = json.loads((run / "coinjoinPipeline_data/unified_report.json").read_text())
    assert report["run_manifest"]["images"]["uploader"] == "uploader@sha256:" + "a" * 64
    assert report["run_manifest"]["image_digests"]["uploader"] == "sha256:" + "a" * 64


@pytest.mark.parametrize("component", ["blocksci", "coinjoin_analysis", "coinjoin_emulator", "uploader"])
@pytest.mark.parametrize("pinned", [False, True])
def test_report_never_fills_producer_digest_from_current_host(tmp_path, monkeypatch, component, pinned):
    run = tmp_path / "run-a"
    report_inputs(run)
    path = run / "blocksci-analysis_data/blocksci_analysis.json"
    data = json.loads(path.read_text())
    producer_digest = "sha256:" + "a" * 64 if pinned else None
    reference = "image@" + producer_digest if pinned else "image:producer"
    data["image_provenance"] = {component: {"reference": reference, "repo_digest": None}}
    path.write_text(json.dumps(data))
    environment_prefix = "COINJOIN_UPLOADER" if component == "uploader" else component.upper()
    monkeypatch.setenv(environment_prefix + "_IMAGE_DIGEST", "sha256:" + "b" * 64)
    monkeypatch.setenv(environment_prefix + "_IMAGE", "image:other")
    with mock.patch("exporters.manifest.docker_image_digest", return_value="sha256:" + "c" * 64) as inspect:
        assert report_cli.main(["--run-dir", str(run)]) == 0
    assert mock.call(reference) not in inspect.call_args_list
    manifest = json.loads((run / "coinjoinPipeline_data/unified_report.json").read_text())["run_manifest"]
    assert manifest["images"][component] == reference
    assert manifest["image_digests"][component] == producer_digest


def test_invalid_timezone_is_an_input_error_before_run_id_generation(capsys):
    assert cli.main(["emulate", "--run-timezone", "missing/timezone", "--dry-run"]) == 2
    assert "invalid run timezone" in capsys.readouterr().err


def test_pbs_resume_failure_does_not_rewrite_original_launcher_manifest(tmp_path):
    run = tmp_path / "run-a"
    run.mkdir()
    manifest = run / "research_manifest.json"
    original = '{"host_launcher": {"status": "completed", "evidence": "original"}}'
    manifest.write_text(original)
    with (
        mock.patch.object(cli, "doctor_check", return_value=[]),
        mock.patch.object(cli, "execute", side_effect=RuntimeError("active job")),
    ):
        assert (
            cli.main(
                [
                    "pbs-from-s3",
                    "--runs-root",
                    str(tmp_path),
                    "--run-id",
                    "run-a",
                    "--blocksciPbs",
                    "--artifact-uri",
                    "s3://bucket/runs",
                    "--s3-endpoint-url",
                    "https://s3.example",
                    "--s3-credentials-file",
                    "/tmp/credentials",
                    "--s3-profile",
                    "test",
                ]
            )
            == 5
        )
    assert manifest.read_text() == original
