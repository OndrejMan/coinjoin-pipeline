from __future__ import annotations

from pathlib import Path
import subprocess
from unittest import mock

import pytest

from coinjoin_pipeline.clean_s3 import (
    CleanError,
    S3Access,
    delete_prefix,
    list_objects,
)


ACCESS = S3Access(
    endpoint_url="https://s3.example.test",
    credentials_file=Path("/storage/user/.aws/credentials"),
    profile="coinjoin",
)


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_list_objects_returns_uris() -> None:
    listing = "s3://bucket/runs/run-1/a\ns3://bucket/runs/run-2/b\n"
    with mock.patch(
        "coinjoin_pipeline.clean_s3._run_s5cmd",
        return_value=_completed(0, stdout=listing),
    ):
        objects = list_objects(ACCESS, "s3://bucket/runs")
    assert objects == [
        "s3://bucket/runs/run-1/a",
        "s3://bucket/runs/run-2/b",
    ]


def test_list_objects_treats_empty_prefix_as_no_objects() -> None:
    with mock.patch(
        "coinjoin_pipeline.clean_s3._run_s5cmd",
        return_value=_completed(1, stderr="ERROR no object found"),
    ):
        assert list_objects(ACCESS, "s3://bucket/runs") == []


def test_list_objects_raises_on_unexpected_error() -> None:
    with mock.patch(
        "coinjoin_pipeline.clean_s3._run_s5cmd",
        return_value=_completed(1, stderr="AccessDenied"),
    ):
        with pytest.raises(CleanError, match="could not inspect"):
            list_objects(ACCESS, "s3://bucket/runs")


def test_delete_prefix_uses_recursive_wildcard() -> None:
    def fake_s5cmd(_access: S3Access, *arguments: str) -> subprocess.CompletedProcess[str]:
        assert arguments == ("rm", "s3://bucket/runs/*")
        return _completed(0)

    with mock.patch(
        "coinjoin_pipeline.clean_s3._run_s5cmd", side_effect=fake_s5cmd
    ):
        delete_prefix(ACCESS, "s3://bucket/runs")


def test_delete_prefix_tolerates_empty_prefix() -> None:
    with mock.patch(
        "coinjoin_pipeline.clean_s3._run_s5cmd",
        return_value=_completed(1, stderr="no object found"),
    ):
        delete_prefix(ACCESS, "s3://bucket/runs")


def test_delete_prefix_raises_on_failure() -> None:
    with mock.patch(
        "coinjoin_pipeline.clean_s3._run_s5cmd",
        return_value=_completed(1, stderr="AccessDenied"),
    ):
        with pytest.raises(CleanError, match="delete failed"):
            delete_prefix(ACCESS, "s3://bucket/runs")
