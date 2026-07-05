import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))

from client.kubernetes import render_s3_emulation_resources  # noqa: E402
from client.pbs import (  # noqa: E402
    render_blocksci_s3_pbs,
    render_coinjoin_analysis_s3_pbs,
    submit_blocksci_s3_pbs,
    submit_coinjoin_analysis_s3_pbs,
)

COMMON = dict(
    artifact_uri="s3://bucket/runs",
    run_id="run-1",
    endpoint_url="https://s3.cl4.du.cesnet.cz",
    credentials_file="/storage/user/.aws/credentials",
    profile="coinjoin",
)


def test_s3_pbs_templates_use_scratch_s5cmd_and_markers() -> None:
    coinjoin = render_coinjoin_analysis_s3_pbs(
        **COMMON, image="docker://coinjoin", command="analyze"
    )
    blocksci = render_blocksci_s3_pbs(
        **COMMON, image="docker://blocksci", command="analyze"
    )
    for script in (coinjoin, blocksci):
        assert "$SCRATCHDIR/coinjoin-run/$RUN_ID" in script
        assert "s5cmd --credentials-file" in script
        assert '--profile "$S3_PROFILE"' in script
        assert '--endpoint-url "$S3_ENDPOINT_URL"' in script
        assert "/storage:/storage" not in script
        assert ".failed" in script and ".done" in script
        assert "aws s3" not in script and "s3cmd" not in script
    assert '"$RUN_WORK/bitcoin_data:/mnt/data:ro"' in blocksci
    assert "requires coinjoin-analysis_data/coinjoin_tx_info.json" in blocksci


def test_frontend_submit_does_not_invoke_s5cmd() -> None:
    with (
        mock.patch("client.pbs.require_qsub"),
        mock.patch("client.pbs.submit_pbs", return_value="42.server") as qsub,
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
        mock.patch("client.pbs.submit_pbs", return_value="blocksci.server") as qsub,
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
            'if [[ "$*" == *" sync s3://"* ]]; then mkdir -p "${@: -1}"; fi\n'
        )
        fake_s5cmd.chmod(0o700)
        fake_singularity = bin_dir / "singularity"
        fake_singularity.write_text("#!/bin/sh\nexit 0\n")
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
    manifest = json.loads(
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
        )
    )
    kinds = {item["kind"] for item in manifest["items"]}
    assert {"ServiceAccount", "Role", "RoleBinding", "Job"}.issubset(kinds)
    job = next(item for item in manifest["items"] if item["kind"] == "Job")
    spec = job["spec"]["template"]["spec"]
    assert any(volume.get("emptyDir") == {} for volume in spec["volumes"])
    containers = {container["name"]: container for container in spec["containers"]}
    assert set(containers) == {"controller", "uploader"}
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
