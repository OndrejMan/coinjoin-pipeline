import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))

from client.artifacts import (  # noqa: E402
    PROBE_RUNNING,
    PROBE_TERMINAL,
    PROBE_UNKNOWN,
    ArtifactTransportError,
    S3Access,
    ensure_empty_run_prefix,
    render_s5cmd_sync,
    run_s5cmd,
    s3_object_exists,
    scrubbed_s3_environment,
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
    wait_for_s3_marker,
)

ACCESS = S3Access(
    endpoint_url="https://s3.cl4.du.cesnet.cz",
    credentials_file="/storage/user/.aws/credentials",
    profile="coinjoin",
)


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["s5cmd"], returncode=returncode, stdout="", stderr=stderr)


def test_validates_s3_compatible_parameters() -> None:
    assert validate_artifact_uri("s3://bucket/runs/") == "s3://bucket/runs"
    assert validate_run_id("wasabi-test_001.2") == "wasabi-test_001.2"
    assert (
        validate_s3_endpoint_url("https://s3.cl4.du.cesnet.cz/")
        == "https://s3.cl4.du.cesnet.cz"
    )
    assert validate_credentials_file("/storage/user/.aws/credentials").startswith(
        "/storage/"
    )


@pytest.mark.parametrize("run_id", ["", "../run", "run/id", "run id", "run;id", "a..b"])
def test_rejects_unsafe_run_ids(run_id: str) -> None:
    with pytest.raises(ValueError):
        validate_run_id(run_id)


def test_render_uses_only_s5cmd_and_disables_environment_fallback() -> None:
    command = render_s5cmd_sync('"$SRC"', '"$DST"')
    assert "s5cmd --credentials-file" in command
    assert "--profile" in command and "--endpoint-url" in command
    assert "env -u AWS_ACCESS_KEY_ID" in command
    assert "aws s3" not in command
    assert "s3cmd" not in command


def test_scrubbed_environment_drops_aws_variables_and_keeps_others() -> None:
    environment = {
        "AWS_ACCESS_KEY_ID": "x",
        "AWS_SECRET_ACCESS_KEY": "y",
        "AWS_SESSION_TOKEN": "z",
        "AWS_PROFILE": "p",
        "AWS_DEFAULT_PROFILE": "p",
        "AWS_REGION": "r",
        "AWS_DEFAULT_REGION": "r",
        "PATH": "/usr/bin",
    }
    with mock.patch.dict(os.environ, environment, clear=True):
        scrubbed = scrubbed_s3_environment()
    assert scrubbed == {"PATH": "/usr/bin"}


def test_s3_object_exists_distinguishes_absent_from_errors() -> None:
    with mock.patch("client.artifacts.subprocess.run", return_value=_completed(0)):
        assert s3_object_exists(ACCESS, "s3://bucket/runs/run-1/.k8s/upload.done") is True
    with mock.patch(
        "client.artifacts.subprocess.run",
        return_value=_completed(1, 'ERROR "ls s3://...": no object found'),
    ):
        assert s3_object_exists(ACCESS, "s3://bucket/runs/run-1/.k8s/upload.done") is False
    with mock.patch(
        "client.artifacts.subprocess.run",
        return_value=_completed(1, "InvalidAccessKeyId"),
    ):
        with pytest.raises(ArtifactTransportError, match="InvalidAccessKeyId"):
            s3_object_exists(ACCESS, "s3://bucket/runs/run-1/.k8s/upload.done")


def test_run_s5cmd_reports_missing_binary() -> None:
    with mock.patch("client.artifacts.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(ArtifactTransportError, match="s5cmd is required"):
            run_s5cmd(ACCESS, "ls", "s3://bucket/key")


def test_ensure_empty_run_prefix_rejects_every_existing_artifact() -> None:
    with mock.patch("client.artifacts.s3_object_exists", return_value=True) as exists:
        with pytest.raises(ArtifactTransportError, match="fresh --run-id"):
            ensure_empty_run_prefix(ACCESS, "s3://bucket/runs", "run-1")
    exists.assert_called_once_with(ACCESS, "s3://bucket/runs/run-1/*")
    with mock.patch("client.artifacts.s3_object_exists", return_value=False):
        ensure_empty_run_prefix(ACCESS, "s3://bucket/runs", "run-1")


def _wait(done: str, failed: str, exists, probe=None, timeout: int = 60) -> None:
    with mock.patch("client.artifacts.s3_object_exists", side_effect=exists):
        wait_for_s3_marker(
            "stage", done, failed, ACCESS, timeout_seconds=timeout, poll_interval=0, probe=probe
        )


def test_wait_for_s3_marker_returns_on_done() -> None:
    _wait("s3://b/r/.done", "s3://b/r/.failed", lambda access, uri: uri.endswith(".done"))


def test_wait_for_s3_marker_raises_on_failed_marker() -> None:
    with pytest.raises(ArtifactTransportError, match="stage failed"):
        _wait("s3://b/r/.done", "s3://b/r/.failed", lambda access, uri: uri.endswith(".failed"))


def test_wait_for_s3_marker_times_out() -> None:
    with pytest.raises(ArtifactTransportError, match="Timed out"):
        _wait("s3://b/r/.done", "s3://b/r/.failed", lambda access, uri: False, timeout=0)


def test_wait_for_s3_marker_raises_after_terminal_probe_grace_cycle() -> None:
    probe_calls = []

    def probe() -> str:
        probe_calls.append("probe")
        return PROBE_TERMINAL

    with pytest.raises(ArtifactTransportError, match="ended without marker"):
        _wait("s3://b/r/.done", "s3://b/r/.failed", lambda access, uri: False, probe=probe)
    assert probe_calls == ["probe"]  # one grace cycle after the terminal report


def test_wait_for_s3_marker_keeps_polling_on_unknown_probe() -> None:
    reports = iter([PROBE_UNKNOWN, PROBE_RUNNING, PROBE_TERMINAL])
    outcomes = iter([False, False, False, False, False, False, False, True])

    def exists(access, uri) -> bool:
        return next(outcomes)

    _wait("s3://b/r/.done", "s3://b/r/.failed", exists, probe=lambda: next(reports))
