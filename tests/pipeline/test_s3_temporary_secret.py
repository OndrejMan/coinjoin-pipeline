"""The temporary per-run S3 Secret: created from the frontend's credentials, owned by the Job, deleted after the upload."""

import base64
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from config_support import configuration

from coinjoin_pipeline.execution import kubernetes, s3_emulation, s3_workflow
from coinjoin_pipeline.execution.kubernetes import (
    S3_JOB_OWNED_RESOURCE_TYPES,
    apply_s3_emulation_resources,
    create_temporary_s3_secret,
    delete_temporary_s3_secret,
    kubernetes_s3_auth_preflight,
    render_s3_emulation_resources,
    require_unused_run_id,
    s3_emulation_job_name,
)
from coinjoin_pipeline.storage.s3 import ArtifactTransportError, S3Access, s3_key_pair

KEY_ID = "AKIA-temporary"
SECRET = "very-secret-value"
OK = subprocess.CompletedProcess([], 0, "", "")


def credentials(tmp_path: Path) -> Path:
    path = tmp_path / "credentials"
    path.write_text(
        f"[other]\naws_access_key_id = x\naws_secret_access_key = y\n"
        f"[coinjoin]\naws_access_key_id = {KEY_ID}\naws_secret_access_key = {SECRET}\n",
        encoding="utf-8",
    )
    return path


def emulation_args(tmp_path: Path, **overrides):
    scenario = tmp_path / "scenario.json"
    scenario.write_text('{"name": "fin"}', encoding="utf-8")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("{}", encoding="utf-8")
    values = {
        "engine": "wasabi",
        "scenario": str(scenario),
        "run_timezone": "UTC",
        "kubeconfig": str(kubeconfig),
        "namespace": "shared-ns",
        "run_id": "run-1",
        "artifact_uri": "s3://bucket/runs",
        "s3_endpoint_url": "https://s3.example.invalid",
        "s3_credentials_file": str(credentials(tmp_path)),
        "s3_profile": "coinjoin",
        "reuse_namespace": True,
        "uploader_image": "uploader:test",
        **overrides,
    }
    return configuration(**values)


def test_key_pair_reads_the_selected_profile(tmp_path: Path) -> None:
    assert s3_key_pair(str(credentials(tmp_path)), "coinjoin") == (KEY_ID, SECRET)
    with pytest.raises(ArtifactTransportError, match="no profile missing"):
        s3_key_pair(str(credentials(tmp_path)), "missing")
    with pytest.raises(ArtifactTransportError, match="not found"):
        s3_key_pair(str(tmp_path / "absent"), "coinjoin")


def test_secret_values_go_on_stdin_with_create_not_apply(tmp_path: Path) -> None:
    with mock.patch.object(kubernetes.subprocess, "run", return_value=OK) as run:
        name = create_temporary_s3_secret(tmp_path / "kubeconfig", "shared-ns", "run-1", KEY_ID, SECRET)

    assert name == s3_emulation_job_name("run-1")
    command = run.call_args.args[0]
    assert "create" in command and "apply" not in command
    assert not any(SECRET in part or KEY_ID in part for part in command)
    secret = json.loads(run.call_args.kwargs["input"])
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == name
    assert secret["metadata"]["labels"]["coinjoin.run-id"] == "run-1"
    assert base64.b64decode(secret["data"]["S3_SECRET_ACCESS_KEY"]).decode() == SECRET
    assert base64.b64decode(secret["data"]["S3_ACCESS_KEY_ID"]).decode() == KEY_ID


def test_an_existing_secret_of_that_name_is_never_replaced(tmp_path: Path) -> None:
    exists = subprocess.CompletedProcess([], 1, "", 'Error from server (AlreadyExists): secrets "x" already exists')
    with (
        mock.patch.object(kubernetes.subprocess, "run", return_value=exists) as run,
        pytest.raises(RuntimeError, match="left alone. Choose another run ID"),
    ):
        create_temporary_s3_secret(tmp_path / "kubeconfig", "shared-ns", "run-1", KEY_ID, SECRET)
    assert run.call_count == 1


def test_the_secret_is_deleted_by_the_run_labels_not_by_name(tmp_path: Path) -> None:
    with mock.patch.object(kubernetes.subprocess, "run", return_value=OK) as run:
        delete_temporary_s3_secret(tmp_path / "kubeconfig", "shared-ns", "run-1")
    command = run.call_args.args[0]
    assert command[5:7] == ["delete", "secret"]
    selector = command[command.index("--selector") + 1]
    assert selector == "app.kubernetes.io/name=coinjoin-s3,coinjoin.run-id=run-1"
    assert s3_emulation_job_name("run-1") not in command


@pytest.mark.parametrize("secrets", [True, False])
def test_a_run_id_already_in_the_namespace_stops_the_launch(tmp_path: Path, secrets: bool) -> None:
    taken = subprocess.CompletedProcess([], 0, "pod/btc-node-run-1-abc\nsecret/coinjoin-s3-run-1\n", "")
    with (
        mock.patch.object(kubernetes.subprocess, "run", return_value=taken) as run,
        pytest.raises(ValueError, match="run ID run-1 is already in use in namespace shared-ns .*choose another"),
    ):
        require_unused_run_id(tmp_path / "kubeconfig", "shared-ns", "run-1", secrets=secrets)
    command = run.call_args.args[0]
    assert command[5:7] == ["get", "jobs.batch,pods,secrets" if secrets else "jobs.batch,pods"]
    assert command[command.index("--selector") + 1] == "coinjoin.run-id=run-1"


def test_an_unused_run_id_passes(tmp_path: Path) -> None:
    with mock.patch.object(kubernetes.subprocess, "run", return_value=OK):
        require_unused_run_id(tmp_path / "kubeconfig", "shared-ns", "run-1", secrets=True)


def test_a_refused_secret_stops_the_launch(tmp_path: Path) -> None:
    refused = subprocess.CompletedProcess([], 1, "", "secrets is forbidden")
    with (
        mock.patch.object(kubernetes.subprocess, "run", return_value=refused),
        pytest.raises(RuntimeError, match="secrets is forbidden"),
    ):
        create_temporary_s3_secret(tmp_path / "kubeconfig", "shared-ns", "run-1", KEY_ID, SECRET)


def test_the_job_owns_its_temporary_secret(tmp_path: Path) -> None:
    manifest = render_s3_emulation_resources(
        namespace="shared-ns",
        run_id="run-1",
        scenario_json="{}",
        engine="wasabi",
        image_prefix="ghcr.io/ondrejman/",
        emulator_image="emulator:test",
        uploader_image="uploader:test",
        artifact_uri="s3://bucket/runs",
        endpoint_url="https://s3.example.invalid",
        secret_name=s3_emulation_job_name("run-1"),
    )
    uid = subprocess.CompletedProcess([], 0, "job-uid", "")
    patches = [OK] * (len(S3_JOB_OWNED_RESOURCE_TYPES) + 1)
    with mock.patch.object(kubernetes.subprocess, "run", side_effect=[OK, uid, *patches]) as run:
        apply_s3_emulation_resources(manifest, tmp_path / "kubeconfig", temporary_secret=True)
    patched = [call.args[0][6] for call in run.call_args_list[2:]]
    assert patched == [*S3_JOB_OWNED_RESOURCE_TYPES, "secret"]


def test_a_failed_apply_also_removes_the_temporary_secret(tmp_path: Path) -> None:
    metadata = {
        "name": s3_emulation_job_name("run-1"),
        "namespace": "shared-ns",
        "labels": {"coinjoin.run-id": "run-1"},
    }
    manifest = json.dumps({"items": [{"kind": "Job", "metadata": metadata}]})
    failure = subprocess.CompletedProcess([], 1, "", "quota exceeded")
    with (
        mock.patch.object(kubernetes.subprocess, "run", side_effect=[failure] + [OK] * 10) as run,
        pytest.raises(RuntimeError, match="kubectl apply failed"),
    ):
        apply_s3_emulation_resources(manifest, tmp_path / "kubeconfig", temporary_secret=True)
    deleted = [call.args[0][5:7] for call in run.call_args_list[1:]]
    assert deleted[-1] == ["delete", "secret"]
    assert "--selector" in run.call_args_list[-1].args[0]


def test_emulation_without_a_secret_name_creates_one_for_the_run(tmp_path: Path) -> None:
    args = emulation_args(tmp_path)
    with (
        mock.patch.object(s3_emulation, "resolve_uploader_image", return_value="uploader:test"),
        mock.patch.object(s3_emulation, "create_temporary_s3_secret") as create,
        mock.patch.object(s3_emulation, "apply_s3_emulation_resources") as apply,
    ):
        s3_emulation.run_s3_kubernetes_emulation(args)

    create.assert_called_once_with(Path(args.kubernetes.kubeconfig), "shared-ns", "run-1", KEY_ID, SECRET)
    manifest, _ = apply.call_args.args
    assert apply.call_args.kwargs == {"temporary_secret": True}
    job = next(item for item in json.loads(manifest)["items"] if item["kind"] == "Job")
    references = {
        variable["valueFrom"]["secretKeyRef"]["name"]
        for container in job["spec"]["template"]["spec"]["containers"]
        for variable in container["env"]
        if "secretKeyRef" in variable.get("valueFrom", {})
    }
    assert references == {s3_emulation_job_name("run-1")}
    assert SECRET not in manifest


def test_emulation_with_a_secret_name_uses_the_existing_secret(tmp_path: Path) -> None:
    args = emulation_args(tmp_path, s3_secret_name="coinjoin-s3-credentials")
    with (
        mock.patch.object(s3_emulation, "resolve_uploader_image", return_value="uploader:test"),
        mock.patch.object(s3_emulation, "create_temporary_s3_secret") as create,
        mock.patch.object(s3_emulation, "apply_s3_emulation_resources") as apply,
    ):
        s3_emulation.run_s3_kubernetes_emulation(args)
    create.assert_not_called()
    assert apply.call_args.kwargs == {"temporary_secret": False}
    assert "coinjoin-s3-credentials" in apply.call_args.args[0]


def test_dry_run_names_the_temporary_secret_without_reading_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = emulation_args(tmp_path, dry_run=True)
    with (
        mock.patch.object(s3_emulation, "resolve_uploader_image", return_value="uploader:test"),
        mock.patch.object(s3_emulation, "s3_key_pair") as key_pair,
    ):
        s3_emulation.run_s3_kubernetes_emulation(args)
    key_pair.assert_not_called()
    assert f"Would create the temporary Secret {s3_emulation_job_name('run-1')}" in capsys.readouterr().out


@pytest.mark.parametrize("fails", [False, True])
def test_the_upload_marker_ends_the_temporary_secret(tmp_path: Path, fails: bool) -> None:
    args = emulation_args(tmp_path)
    access = S3Access("https://s3.example.invalid", "/credentials", "coinjoin")
    wait = mock.Mock(side_effect=ArtifactTransportError("upload failed") if fails else None)
    with (
        mock.patch.object(s3_workflow, "wait_for_s3_marker", wait),
        mock.patch.object(s3_workflow, "kubernetes_job_probe"),
        mock.patch.object(s3_workflow, "collect_s3_emulation_diagnostics", return_value=""),
        mock.patch.object(s3_workflow, "delete_s3_emulation_job"),
        mock.patch.object(s3_workflow, "delete_temporary_s3_secret") as delete,
    ):
        if fails:
            with pytest.raises(ArtifactTransportError):
                s3_workflow.wait_for_emulation_upload(args, "s3://bucket/runs/run-1", access)
        else:
            s3_workflow.wait_for_emulation_upload(args, "s3://bucket/runs/run-1", access)
    delete.assert_called_once_with(Path(args.kubernetes.kubeconfig), "shared-ns", "run-1")


def test_a_named_secret_outlives_the_run(tmp_path: Path) -> None:
    args = emulation_args(tmp_path, s3_secret_name="coinjoin-s3-credentials")
    access = S3Access("https://s3.example.invalid", "/credentials", "coinjoin")
    with (
        mock.patch.object(s3_workflow, "wait_for_s3_marker"),
        mock.patch.object(s3_workflow, "kubernetes_job_probe"),
        mock.patch.object(s3_workflow, "delete_temporary_s3_secret") as delete,
    ):
        s3_workflow.wait_for_emulation_upload(args, "s3://bucket/runs/run-1", access)
    delete.assert_not_called()


def _checked_permissions(secret_name: str | None) -> tuple[list[tuple[str, str]], list[tuple[str, ...]]]:
    permissions: list[tuple[str, str]] = []
    reads: list[tuple[str, ...]] = []

    def preflight(_kubeconfig: Path, *arguments: str, namespace: str | None = None) -> str:
        if arguments[:2] == ("auth", "can-i"):
            permissions.append((arguments[2], arguments[3]))
            return "yes"
        reads.append(arguments)
        return ""

    with mock.patch.object(kubernetes, "_preflight", side_effect=preflight):
        kubernetes_s3_auth_preflight(Path("/kubeconfig"), "shared-ns", True, secret_name)
    return permissions, reads


def test_preflight_of_a_temporary_secret_checks_the_right_to_manage_secrets() -> None:
    permissions, reads = _checked_permissions(None)
    assert {("create", "secrets"), ("list", "secrets"), ("patch", "secrets"), ("delete", "secrets")} <= set(permissions)
    assert {("list", "jobs.batch"), ("list", "pods")} <= set(permissions)
    assert not any(read[:2] == ("get", "secret") for read in reads)


def test_preflight_of_a_named_secret_requires_that_it_exists() -> None:
    permissions, reads = _checked_permissions("coinjoin-s3-credentials")
    assert ("create", "secrets") not in permissions
    assert ("get", "secret", "coinjoin-s3-credentials") in reads
