"""Notebook connection handoff with mocked S3; no PBS, network or containers."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coinjoin_pipeline.configuration import BlockSciConfiguration
from coinjoin_pipeline.execution.pbs.analysis_modes import analysis_mode_settings
from coinjoin_pipeline.execution.pbs.blocksci_analysis_jobs import notebook_connection


def stage():
    return SimpleNamespace(run_id="r1", run_prefix="s3://bucket/runs/r1", storage=Mock(), log=Mock())


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
        "blocksci-notebook", BlockSciConfiguration(notebooks_dir="/storage/not-present-locally/notebooks")
    )
    assert settings["output"]["notebooks_dir"] == "/storage/not-present-locally/notebooks"
