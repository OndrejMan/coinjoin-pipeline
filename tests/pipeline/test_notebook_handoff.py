"""Notebook connection/seed handoff with mocked S3; no PBS, network or containers."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coinjoin_pipeline.execution.pbs.analysis_modes import analysis_mode_settings
from coinjoin_pipeline.execution.pbs.stage_runtime import StageFailure, notebook_connection, seed_notebook


def stage():
    return SimpleNamespace(run_id="r1", run_prefix="s3://bucket/runs/r1", storage=Mock(), log=Mock())


def test_seed_is_exclusive_and_rejects_paths(tmp_path):
    job = stage()
    job.storage.cat_json.return_value = {"name": "analysis.py", "text": "print(1)"}
    seed_notebook(job, tmp_path)
    output = tmp_path / "analysis.py"
    assert output.read_text() == "print(1)"
    assert output.stat().st_mode & 0o777 == 0o600
    output.write_text("user edits")
    with pytest.raises(FileExistsError):
        seed_notebook(job, tmp_path)
    assert output.read_text() == "user edits"
    job.storage.cat_json.return_value = {"name": "../escape.py", "text": "x"}
    with pytest.raises(StageFailure, match="Invalid initial"):
        seed_notebook(job, tmp_path)


def test_missing_seed_does_not_write_anything(tmp_path):
    job = stage()
    job.storage.exists.return_value = False
    seed_notebook(job, tmp_path)
    assert not list(tmp_path.iterdir())
    job.storage.cat_json.assert_not_called()


def test_publishes_only_current_pbs_job_and_removes_connection_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PBS_JOBID", "12.pbs")
    job = stage()
    published = threading.Event()
    job.storage.copy.side_effect = lambda *_args, **_kwargs: published.set()
    connection = tmp_path / "connection.json"
    connection.write_text(json.dumps({"runId": "r1", "jobId": "old.pbs", "token": "private"}))
    with pytest.raises(RuntimeError, match="kernel stopped"), notebook_connection(job, tmp_path):
        assert not published.wait(0.25)
        connection.write_text(json.dumps({"runId": "r1", "jobId": "12.pbs", "token": "private"}))
        assert published.wait(1)
        raise RuntimeError("kernel stopped")
    job.storage.copy.assert_called_once_with(connection, "s3://bucket/runs/r1/.notebook/connection.json", timeout=15)
    job.storage.remove.assert_called_once_with("s3://bucket/runs/r1/.notebook/connection.json", timeout=15)
    assert "private" not in str(job.log.call_args_list)


def test_publication_retries_and_cleanup_error_does_not_mask_kernel_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PBS_JOBID", "12.pbs")
    job = stage()
    published = threading.Event()

    def copy(*_args, **_kwargs):
        if job.storage.copy.call_count == 1:
            raise OSError("unavailable")
        published.set()

    job.storage.copy.side_effect = copy
    job.storage.remove.side_effect = OSError("unavailable")
    (tmp_path / "connection.json").write_text(json.dumps({"runId": "r1", "jobId": "12.pbs"}))
    with pytest.raises(RuntimeError, match="kernel failed"), notebook_connection(job, tmp_path):
        assert published.wait(2)
        raise RuntimeError("kernel failed")


def test_notebook_directory_is_validated_on_compute_node_not_submitting_container():
    settings = analysis_mode_settings(
        "blocksci-notebook",
        user_script=None,
        external_baseline_uri=None,
        external_false_cjtxs_uri=None,
        notebooks_dir=Path("/storage/not-present-locally/notebooks"),
        notebook_port=8888,
    )
    assert settings["output"]["notebooks_dir"] == "/storage/not-present-locally/notebooks"
