"""The stage worker's lifecycle: outputs, then the terminal marker, then an empty scratch."""

import subprocess

import pytest

from coinjoin_pipeline.execution.pbs import stage_runtime
from coinjoin_pipeline.execution.pbs.stage_runtime import Stage


def stage(tmp_path, events, *, transfer_status=0, marker_status=0):
    settings = {
        "stage": "blocksci",
        "job": "blocksci",
        "run_id": "run-1",
        "run_prefix": "s3://bucket/runs/run-1",
        "endpoint": "https://s3.example.invalid",
        "credentials": "/dev/null",
        "profile": "test",
    }
    job = Stage(settings, tmp_path / "scratch")

    def outputs():
        events.append("outputs")
        if transfer_status:
            raise subprocess.CalledProcessError(transfer_status, "s5cmd sync")

    def publish_marker(outcome):
        events.append(outcome)
        if outcome == "done" and marker_status:
            raise subprocess.CalledProcessError(marker_status, "s5cmd cp")

    job.finalizers.append(outputs)
    job.publish_marker = publish_marker
    return job


@pytest.mark.parametrize(
    "payload_status,transfer_status,marker_status,expected",
    [
        (0, 0, 0, 0),
        (7, 0, 0, 7),
        (0, 9, 0, 9),
        (7, 9, 0, 7),
        (0, 0, 8, 8),
    ],
)
def test_terminal_marker_follows_outputs_and_preserves_failure(
    tmp_path, payload_status, transfer_status, marker_status, expected
):
    events = []
    job = stage(tmp_path, events, transfer_status=transfer_status, marker_status=marker_status)

    assert job.finish(job.finalize(payload_status)) == expected
    assert events[0] == "outputs"
    assert events[-1] == ("done" if expected == 0 else "failed")


def filled_scratch(tmp_path):
    scratch = tmp_path / "scratch"
    (scratch / "work").mkdir(parents=True)
    (scratch / "work/index").touch()
    (scratch / ".hidden").touch()
    return scratch


def test_scratch_contents_are_removed_and_the_directory_stays(tmp_path, monkeypatch):
    monkeypatch.setattr(stage_runtime.shutil, "which", lambda name: None)
    scratch = filled_scratch(tmp_path)

    stage_runtime.clean_scratch(scratch)

    assert scratch.is_dir() and list(scratch.iterdir()) == []


def test_scratch_cleanup_prefers_metacentrum_clean_scratch(tmp_path, monkeypatch):
    scratch = filled_scratch(tmp_path)
    calls = []
    monkeypatch.setattr(stage_runtime.shutil, "which", lambda name: "/usr/bin/clean_scratch")
    monkeypatch.setattr(stage_runtime.subprocess, "run", lambda command, check: calls.append(command))

    stage_runtime.clean_scratch(scratch)

    assert calls == [["clean_scratch"]]
    assert (scratch / "work/index").exists()


def test_a_missing_scratch_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(stage_runtime.shutil, "which", lambda name: None)

    stage_runtime.clean_scratch(tmp_path / "missing")


def test_a_job_without_scratch_fails_before_doing_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SCRATCHDIR", raising=False)

    assert stage_runtime.main([str(tmp_path / "settings.json")]) == 1
    assert "SCRATCHDIR is not set" in capsys.readouterr().err
