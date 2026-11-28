"""The copy of a run's launch record in its S3 prefix: when it is uploaded and what a resume keeps."""

import json
from unittest import mock

import pytest
from config_support import configuration

from coinjoin_pipeline.execution import s3_run_manifest
from coinjoin_pipeline.runs import store_host_manifest
from coinjoin_pipeline.storage.s3 import ArtifactTransportError

URI = "s3://bucket/runs/run-1/research_manifest.json"


def _args(tmp_path):
    return configuration(
        {
            "action": "full-run",
            "artifacts.backend": "s3",
            "driver": "kubernetes",
            "artifacts.uri": "s3://bucket/runs",
            "run_id": "run-1",
            "runs_root": tmp_path,
            "artifacts.endpoint_url": "https://s3.example",
            "artifacts.credentials_file": "/credentials",
            "artifacts.profile": "coinjoin",
            "artifacts.secret_name": "coinjoin-s3",
            "kubernetes.namespace": "coinjoin-ns",
            "kubernetes.reuse_namespace": True,
        }
    )


@pytest.fixture
def uploads(tmp_path):
    """What each upload carried: the object and the launches the record held at that moment."""
    sent = []

    def upload(_access, source, uri, **_options):
        record = json.loads(source.read_text())
        earlier = [item["launch_id"] for item in record.get("earlier_host_launchers", [])]
        sent.append((uri, earlier, record["host_launcher"]["launch_id"], record["host_launcher"]["status"]))

    s3_run_manifest._PUBLISHED.clear()
    with mock.patch.object(s3_run_manifest, "upload_s3_file", side_effect=upload):
        yield sent
    s3_run_manifest._PUBLISHED.clear()


def _record(tmp_path, launch_id="local", status="prepared"):
    target = tmp_path / "run-1/research_manifest.json"
    store_host_manifest(target, {"launch_id": launch_id, "status": status})
    return target


def test_a_new_run_uploads_its_record_and_again_with_its_outcome(tmp_path, uploads) -> None:
    target = _record(tmp_path)
    with mock.patch.object(s3_run_manifest, "s3_json") as remote:
        s3_run_manifest.publish_run_manifest(_args(tmp_path))
    # A new run's prefix was just found empty, so there is nothing to read back.
    remote.assert_not_called()

    store_host_manifest(target, {"launch_id": "local", "status": "completed"})
    s3_run_manifest.republish_run_manifests()
    s3_run_manifest.republish_run_manifests()

    assert uploads == [(URI, [], "local", "prepared"), (URI, [], "local", "completed")]


def test_a_run_without_a_launch_record_publishes_nothing(tmp_path, uploads) -> None:
    s3_run_manifest.publish_run_manifest(_args(tmp_path))
    s3_run_manifest.republish_run_manifests()
    assert uploads == []


def test_a_continued_run_keeps_the_launches_only_s3_records(tmp_path, uploads) -> None:
    target = _record(tmp_path, "resume")
    recorded = {
        "host_launcher": {"launch_id": "second", "status": "failed"},
        "earlier_host_launchers": [{"launch_id": "first", "status": "failed"}],
    }
    with (
        mock.patch.object(s3_run_manifest, "s3_prefix_has_objects", return_value=True),
        mock.patch.object(s3_run_manifest, "s3_json", return_value=recorded),
    ):
        s3_run_manifest.publish_run_manifest(_args(tmp_path), continued=True)
        # A second resume from this directory already holds them.
        s3_run_manifest.publish_run_manifest(_args(tmp_path), continued=True)

    assert uploads == [(URI, ["first", "second"], "resume", "prepared")] * 2
    assert json.loads(target.read_text())["host_launcher"]["launch_id"] == "resume"


def test_a_continued_run_leaves_an_empty_prefix_empty(tmp_path, uploads) -> None:
    _record(tmp_path)
    with mock.patch.object(s3_run_manifest, "s3_prefix_has_objects", return_value=False):
        s3_run_manifest.publish_run_manifest(_args(tmp_path), continued=True)
    s3_run_manifest.republish_run_manifests()
    assert uploads == []


def test_an_unreadable_copy_is_replaced(tmp_path, uploads, capsys) -> None:
    _record(tmp_path)
    with (
        mock.patch.object(s3_run_manifest, "s3_prefix_has_objects", return_value=True),
        mock.patch.object(s3_run_manifest, "s3_json", side_effect=json.JSONDecodeError("bad", "{", 0)),
    ):
        s3_run_manifest.publish_run_manifest(_args(tmp_path), continued=True)
    assert uploads == [(URI, [], "local", "prepared")]
    assert "is unreadable and is replaced" in capsys.readouterr().err


def test_a_failed_last_upload_only_warns(tmp_path, uploads, capsys) -> None:
    _record(tmp_path)
    s3_run_manifest.publish_run_manifest(_args(tmp_path))
    with mock.patch.object(s3_run_manifest, "upload_s3_file", side_effect=ArtifactTransportError("s5cmd cp failed")):
        s3_run_manifest.republish_run_manifests()
    assert "still records a running launch: s5cmd cp failed" in capsys.readouterr().err
    assert not s3_run_manifest._PUBLISHED
