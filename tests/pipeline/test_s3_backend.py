import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))

from client.kubernetes import render_s3_emulation_resources  # noqa: E402
from client.pbs import (  # noqa: E402
    blocksci_export_pbs_command,
    render_blocksci_s3_pbs,
    render_coinjoin_analysis_s3_pbs,
    render_unified_report_s3_pbs,
    submit_blocksci_s3_pbs,
    submit_coinjoin_analysis_s3_pbs,
    submit_unified_report_s3_pbs,
)
from client.wrapper import run_pbs_from_s3  # noqa: E402

COMMON = dict(
    artifact_uri="s3://bucket/runs",
    run_id="run-1",
    endpoint_url="https://s3.cl4.du.cesnet.cz",
    credentials_file="/storage/user/.aws/credentials",
    profile="coinjoin",
)


def render_kubernetes_manifest(*, reuse_namespace: bool = False) -> dict:
    return json.loads(
        render_s3_emulation_resources(
            namespace="coinjoin",
            run_id="run-1",
            scenario_json="{}",
            engine="wasabi",
            image_prefix="ghcr.io/ondrejman/",
            emulator_image="emulator:latest",
            uploader_image="pipeline:latest",
            artifact_uri="s3://bucket/runs",
            endpoint_url="https://s3.cl4.du.cesnet.cz",
            secret_name="coinjoin-s3",
            reuse_namespace=reuse_namespace,
        )
    )


def s3_pbs_args(*, analysis: bool = True, blocksci: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_uri=COMMON["artifact_uri"],
        run_id=COMMON["run_id"],
        s3_endpoint_url=COMMON["endpoint_url"],
        s3_credentials_file=COMMON["credentials_file"],
        s3_profile=COMMON["profile"],
        dry_run=False,
        analysisPbs=analysis,
        blocksciPbs=blocksci,
        coinjoin_type="wasabi2",
        min_input_count=2,
        joinmarket_detector="definite",
        joinmarket_min_base_fee=5000,
        joinmarket_percentage_fee=0.00004,
        joinmarket_max_depth=200000,
        test_values=True,
    )


def test_s3_pbs_templates_use_scratch_s5cmd_and_markers() -> None:
    coinjoin = render_coinjoin_analysis_s3_pbs(
        **COMMON, image="docker://coinjoin", command="analyze"
    )
    blocksci = render_blocksci_s3_pbs(
        **COMMON, image="docker://blocksci", command="analyze"
    )
    report = render_unified_report_s3_pbs(
        **COMMON, image="docker://blocksci", command="report"
    )
    for script in (coinjoin, blocksci, report):
        assert "$SCRATCHDIR/coinjoin-run/$RUN_ID" in script
        assert "s5cmd --credentials-file" in script
        assert '--profile "$S3_PROFILE"' in script
        assert '--endpoint-url "$S3_ENDPOINT_URL"' in script
        assert "/storage:/storage" not in script
        assert ".failed" in script and ".done" in script
        assert "aws s3" not in script and "s3cmd" not in script
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert '"$CONTAINER_WORK_ROOT:/runs/emulation/selected:rw"' in coinjoin
    assert (
        '"$RUN_WORK/coinjoin-analysis_data:/runs/emulation/selected/$RUN_ID:rw"'
        in coinjoin
    )
    assert (
        '"$RUN_WORK/coinjoin_emulator_data/data:/runs/emulation/selected/$RUN_ID/data:ro"'
        in coinjoin
    )
    assert '"$RUN_WORK:/runs/emulation/selected/$RUN_ID:rw"' not in coinjoin
    assert "did not produce coinjoin-analysis_data/coinjoin_tx_info.json" in coinjoin
    assert 'BITCOIN_DATADIR="$RUN_WORK/bitcoin_data"' in blocksci
    assert 'BITCOIN_DATADIR="$BITCOIN_DATADIR/data"' in blocksci
    assert '"$BITCOIN_DATADIR:/mnt/data:ro"' in blocksci
    assert "requires a Bitcoin datadir containing regtest/blocks" in blocksci
    assert "requires coinjoin-analysis_data/coinjoin_tx_info.json" in blocksci
    assert "Unified S3 report requires blocksci_data/config.json" in report
    assert "Unified S3 report requires coinjoin-analysis_data/coinjoin_tx_info.json" in report
    assert "#PBS -l select=1:ncpus=8:mem=64gb:scratch_local=100gb" in blocksci
    assert "#PBS -l select=1:ncpus=2:mem=8gb:scratch_local=100gb" in report
    for script in (blocksci, report):
        assert 'REPORT_DIR="$RUN_WORK/coinjoinPipeline_data"' in script
        assert 'sync "$REPORT_DIR/" "$ARTIFACT_URI/$RUN_ID/coinjoinPipeline_data/"' in script
        assert "blocksciEmulatorAnalysis_data" not in script
    assert "/mnt/data" not in report


def test_wrapper_images_package_unified_report_s3_template() -> None:
    for dockerfile in (
        PROJECT_ROOT / "Dockerfile",
        PROJECT_ROOT / "pipeline" / "client" / "Dockerfile",
    ):
        assert "unified_report_s3_template.sh" in dockerfile.read_text(
            encoding="utf-8"
        )


def test_blocksci_s3_parse_only_does_not_require_or_upload_report() -> None:
    blocksci = render_blocksci_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command="parse",
        include_report=False,
    )

    assert "requires coinjoin-analysis_data/coinjoin_tx_info.json" not in blocksci
    assert "coinjoinPipeline_data/" not in blocksci
    assert "blocksciEmulatorAnalysis_data/" not in blocksci
    assert "REPORT_DIR=" not in blocksci
    assert "blocksci_data/" in blocksci


def test_frontend_submit_does_not_invoke_s5cmd() -> None:
    with (
        mock.patch("client.pbs.require_qsub"),
        mock.patch("client.pbs.submit_pbs_text", return_value="42.server") as qsub,
        mock.patch("subprocess.run") as run,
    ):
        assert (
            submit_coinjoin_analysis_s3_pbs(
                **COMMON, image="docker://coinjoin", command="analyze"
            )
            == "42.server"
        )
    qsub.assert_called_once()
    run.assert_not_called()


def test_blocksci_submission_forwards_analysis_dependency() -> None:
    with (
        mock.patch("client.pbs.require_qsub"),
        mock.patch("client.pbs.submit_pbs_text", return_value="blocksci.server") as qsub,
    ):
        assert (
            submit_blocksci_s3_pbs(
                **COMMON,
                image="docker://blocksci",
                command="analyze",
                dependency_job_id="analysis.server",
            )
            == "blocksci.server"
        )
    assert qsub.call_args.args[1] == "analysis.server"


def test_unified_report_submission_forwards_both_dependencies() -> None:
    with (
        mock.patch("client.pbs.require_qsub"),
        mock.patch("client.pbs.submit_pbs_text", return_value="report.server") as qsub,
    ):
        assert (
            submit_unified_report_s3_pbs(
                **COMMON,
                image="docker://blocksci",
                command="report",
                dependency_job_ids=("analysis.server", "blocksci.server"),
            )
            == "report.server"
        )
    assert qsub.call_args.args[1] == ("analysis.server", "blocksci.server")


def test_pbs_from_s3_submits_parallel_analyzers_then_dependent_report() -> None:
    args = s3_pbs_args()
    with (
        mock.patch(
            "client.wrapper.submit_coinjoin_analysis_s3_pbs",
            return_value="analysis.server",
        ) as analysis,
        mock.patch(
            "client.wrapper.submit_blocksci_s3_pbs",
            return_value="blocksci.server",
        ) as blocksci,
        mock.patch(
            "client.wrapper.submit_unified_report_s3_pbs",
            return_value="report.server",
        ) as report,
    ):
        run_pbs_from_s3(args)

    analysis.assert_called_once()
    blocksci.assert_called_once()
    report.assert_called_once()
    assert blocksci.call_args.kwargs["include_report"] is False
    assert "unified_report.py" not in blocksci.call_args.kwargs["command"]
    assert report.call_args.kwargs["dependency_job_ids"] == (
        "analysis.server",
        "blocksci.server",
    )
    assert report.call_args.kwargs["ncpus"] == 2
    assert report.call_args.kwargs["mem"] == "8gb"
    assert report.call_args.kwargs["scratch"] == "100gb"
    assert report.call_args.kwargs["walltime"] == "24:00:00"
    assert report.call_args.kwargs["command"] == blocksci_export_pbs_command(
        run_id="run-1",
        coinjoin_type="wasabi2",
        min_input_count=2,
        joinmarket_detector="definite",
        joinmarket_min_base_fee=5000,
        joinmarket_percentage_fee=0.00004,
        joinmarket_max_depth=200000,
        test_values=True,
    )


def test_pbs_from_s3_report_specific_resources_override_shared_resources() -> None:
    args = s3_pbs_args()
    args.pbs_ncpus = 6
    args.pbs_mem = "24gb"
    args.pbs_scratch = "120gb"
    args.pbs_walltime = "12:00:00"
    args.pbs_unified_report_ncpus = 1
    args.pbs_unified_report_mem = "4gb"
    args.pbs_unified_report_scratch = "20gb"
    args.pbs_unified_report_walltime = "01:00:00"
    with (
        mock.patch(
            "client.wrapper.submit_coinjoin_analysis_s3_pbs",
            return_value="analysis.server",
        ) as analysis,
        mock.patch(
            "client.wrapper.submit_blocksci_s3_pbs",
            return_value="blocksci.server",
        ) as blocksci,
        mock.patch(
            "client.wrapper.submit_unified_report_s3_pbs",
            return_value="report.server",
        ) as report,
    ):
        run_pbs_from_s3(args)

    assert analysis.call_args.kwargs["ncpus"] == 6
    assert analysis.call_args.kwargs["mem"] == "24gb"
    assert blocksci.call_args.kwargs["ncpus"] == 6
    assert blocksci.call_args.kwargs["mem"] == "24gb"
    assert report.call_args.kwargs["ncpus"] == 1
    assert report.call_args.kwargs["mem"] == "4gb"
    assert report.call_args.kwargs["scratch"] == "20gb"
    assert report.call_args.kwargs["walltime"] == "01:00:00"


def test_pbs_from_s3_blocksci_only_keeps_combined_report() -> None:
    args = s3_pbs_args(analysis=False)
    with (
        mock.patch(
            "client.wrapper.submit_blocksci_s3_pbs",
            return_value="blocksci.server",
        ) as blocksci,
        mock.patch("client.wrapper.submit_unified_report_s3_pbs") as report,
    ):
        run_pbs_from_s3(args)

    blocksci.assert_called_once()
    report.assert_not_called()
    assert blocksci.call_args.kwargs["include_report"] is True
    assert "unified_report.py" in blocksci.call_args.kwargs["command"]


def test_s3_submission_pipes_script_to_qsub_stdin() -> None:
    with (
        mock.patch("client.pbs.require_qsub"),
        mock.patch("client.pbs.subprocess.run") as run,
    ):
        run.return_value = mock.Mock(returncode=0, stdout="7.server\n", stderr="")
        job_id = submit_blocksci_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command="analyze",
            dependency_job_id="analysis.server",
        )
    assert job_id == "7.server"
    argv = run.call_args.args[0]
    assert argv[0] == "qsub"
    assert ["-W", "depend=afterok:analysis.server"] == argv[1:3]
    assert len(argv) == 3  # no script path argument; the script travels via stdin
    assert "#PBS" in run.call_args.kwargs["input"]


def test_rendered_pbs_script_calls_fake_s5cmd_only_on_compute_path() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        bin_dir = root / "bin"
        scratch = root / "scratch"
        bin_dir.mkdir()
        scratch.mkdir()
        credentials = root / "credentials"
        credentials.write_text(
            "[coinjoin]\naws_access_key_id=x\naws_secret_access_key=y\n"
        )
        calls = root / "s5cmd.calls"
        fake_s5cmd = bin_dir / "s5cmd"
        fake_s5cmd.write_text(
            "#!/bin/bash\n"
            'printf "%s\\n" "$*" >> "$S5CMD_CALLS"\n'
            'if [[ "$*" == *" sync s3://"* ]]; then '
            'mkdir -p "${@: -1}/coinjoin_emulator_data/data"; fi\n'
        )
        fake_s5cmd.chmod(0o700)
        fake_singularity = bin_dir / "singularity"
        fake_singularity.write_text(
            "#!/bin/bash\n"
            'for argument in "$@"; do\n'
            '  case "$argument" in\n'
            '    *coinjoin-analysis_data:/runs/emulation/selected/*:rw)\n'
            '      output_dir="${argument%%:*}"\n'
            '      printf \'{"coinjoins": {}}\\n\' > "$output_dir/coinjoin_tx_info.json"\n'
            "      ;;\n"
            "  esac\n"
            "done\n"
        )
        fake_singularity.chmod(0o700)
        script = render_coinjoin_analysis_s3_pbs(
            artifact_uri="s3://bucket/runs",
            run_id="run-1",
            endpoint_url="https://s3.cl4.du.cesnet.cz",
            credentials_file=str(credentials),
            profile="coinjoin",
            image="docker://coinjoin",
            command="true",
        )
        script_path = root / "job.pbs"
        script_path.write_text(script)
        environment = os.environ | {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SCRATCHDIR": str(scratch),
            "S5CMD_CALLS": str(calls),
        }
        subprocess.run(["bash", str(script_path)], env=environment, check=True)
        logged = calls.read_text()
        assert "sync s3://bucket/runs/run-1/*" in logged
        assert "sync " in logged and "coinjoin-analysis_data" in logged
        assert "cp " in logged and "coinjoin-analysis.done" in logged


def test_kubernetes_manifest_has_controller_uploader_secret_and_rbac() -> None:
    manifest = render_kubernetes_manifest()
    kinds = {item["kind"] for item in manifest["items"]}
    assert {"ServiceAccount", "Role", "RoleBinding", "Job"}.issubset(kinds)
    assert "ClusterRole" not in kinds
    assert "ClusterRoleBinding" not in kinds
    rbac = [
        item
        for item in manifest["items"]
        if item["apiVersion"] == "rbac.authorization.k8s.io/v1"
    ]
    assert {item["kind"] for item in rbac} == {"Role", "RoleBinding"}
    assert all(item["metadata"]["namespace"] == "coinjoin" for item in rbac)
    role_binding = next(item for item in rbac if item["kind"] == "RoleBinding")
    assert role_binding["roleRef"]["kind"] == "Role"
    role = next(item for item in rbac if item["kind"] == "Role")
    permissions = {
        resource: set(rule["verbs"])
        for rule in role["rules"]
        for resource in rule["resources"]
    }
    assert permissions["pods/status"] == {"get"}
    assert {"get", "list", "watch"}.issubset(permissions["events"])

    job = next(item for item in manifest["items"] if item["kind"] == "Job")
    assert job["spec"]["ttlSecondsAfterFinished"] == 3600
    spec = job["spec"]["template"]["spec"]
    assert spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }

    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    assert volumes["artifacts"]["emptyDir"] == {}
    assert volumes["credentials"]["emptyDir"] == {"medium": "Memory"}

    init_containers = {container["name"]: container for container in spec["initContainers"]}
    assert set(init_containers) == {"prefix-preflight"}
    prefix_preflight = init_containers["prefix-preflight"]
    assert "already contains artifacts" in prefix_preflight["command"][-1]
    assert "no object found" in prefix_preflight["command"][-1]
    subprocess.run(
        ["bash", "-n"], input=prefix_preflight["command"][-1], text=True, check=True
    )
    assert prefix_preflight["resources"] == {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }

    containers = {container["name"]: container for container in spec["containers"]}
    assert set(containers) == {"controller", "uploader"}
    expected_resources = {
        "controller": {
            "requests": {"cpu": "250m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        },
        "uploader": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        },
    }
    for container_name, container in containers.items():
        security_context = container["securityContext"]
        assert security_context["allowPrivilegeEscalation"] is False
        assert security_context["capabilities"]["drop"] == ["ALL"]
        assert "privileged" not in security_context
        assert container["resources"] == expected_resources[container_name]
        assert any(mount["name"] == "artifacts" for mount in container["volumeMounts"])

    assert any(
        mount["name"] == "credentials"
        for mount in containers["uploader"]["volumeMounts"]
    )
    rendered = json.dumps(manifest)
    assert (
        "s5cmd" in rendered
        and "upload.done" in rendered
        and "upload.failed" in rendered
    )
    assert "coinjoin-s3" in rendered
    assert "<access" not in rendered and "secret_key" not in rendered
    assert "POD_NAME" in rendered
    assert "metadata.name" in rendered
    assert "state.terminated.exitCode" in rendered
    assert "ImagePullBackOff" in rendered
    assert 's5 cp \\"/artifacts/$RUN_ID/.k8s/upload.failed\\"' in rendered


def test_kubernetes_manifest_reuses_existing_namespace() -> None:
    manifest = render_kubernetes_manifest(reuse_namespace=True)

    assert all(item["kind"] != "Namespace" for item in manifest["items"])
    assert all(
        item["metadata"].get("namespace") == "coinjoin" for item in manifest["items"]
    )
