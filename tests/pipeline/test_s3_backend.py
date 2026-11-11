import json
import os
from dataclasses import replace
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from coinjoin_pipeline.configuration import PipelineConfiguration
from types import SimpleNamespace
from unittest import mock

import pytest
from config_support import build_parser, configuration

from coinjoin_pipeline.context import resolve_configuration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))

from coinjoin_pipeline.execution.locks import acquire_lock
from coinjoin_pipeline.execution.kubernetes import (  # noqa: E402
    S3_JOB_OWNED_RESOURCE_TYPES,
    apply_s3_emulation_resources,
    render_s3_emulation_resources,
    s3_emulation_job_name,
)
from coinjoin_pipeline.execution.pbs.commands import (
    blocksci_analysis_pbs_command,
    blocksci_external_report_pbs_command,
    blocksci_notebook_pbs_command,
    blocksci_parse_pbs_command,
    blocksci_update_pbs_command,
)
from coinjoin_pipeline.execution.pbs.index_store import IndexStore
from coinjoin_pipeline.execution.pbs.blocksci_jobs import publish_persistent
from coinjoin_pipeline.execution.pbs.stage import Stage
from coinjoin_pipeline.execution.pbs.templates_s3 import (
    PersistentIndex,
    render_blocksci_analyze_s3_pbs,
    render_blocksci_index_backup_s3_pbs,
    render_coinjoin_analysis_dumplings_s3_pbs,
    render_blocksci_parse_s3_pbs,
    render_blocksci_s3_pbs,
    render_blocksci_update_s3_pbs,
    render_coinjoin_analysis_s3_pbs,
    render_unified_report_s3_pbs,
)
from coinjoin_pipeline.execution.pbs.scripts import embedded_file
from coinjoin_pipeline.execution.pbs.defaults import BLOCKSCI_IMAGE_PYTHON_COMMAND, DEFAULT_RESOURCES
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.s3_emulation import run_s3_kubernetes_emulation
from coinjoin_pipeline.execution.s3_markers import rollback_s3_pbs_submissions
from coinjoin_pipeline.execution.s3_staging import pbs_stages_need_exporters
from coinjoin_pipeline.storage.s3 import (  # noqa: E402
    ArtifactTransportError,
    S3Target,
)


def settings_of(script: str) -> dict:
    """The settings.json a rendered job hands its worker."""
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    return json.loads(embedded_file(script, "settings.json"))


S3_TARGET = S3Target(
    artifact_uri="s3://bucket/runs",
    run_id="run-1",
    endpoint_url="https://s3.cl4.du.cesnet.cz",
    credentials_file="/storage/user/.aws/credentials",
    profile="coinjoin",
)
COMMON = dict(target=S3_TARGET)
ANALYSIS = DEFAULT_RESOURCES["analysis"]


_SUBMISSION_DIR: list[Path] = []


def render_kubernetes_manifest(*, reuse_namespace: bool = False, engine: str = "wasabi") -> dict:
    return json.loads(
        render_s3_emulation_resources(
            namespace="coinjoin",
            run_id="run-1",
            scenario_json="{}",
            engine=engine,
            image_prefix="ghcr.io/ondrejman/",
            emulator_image="emulator:latest",
            uploader_image="pipeline:latest",
            artifact_uri="s3://bucket/runs",
            endpoint_url="https://s3.cl4.du.cesnet.cz",
            secret_name="coinjoin-s3",
            reuse_namespace=reuse_namespace,
        )
    )


def test_s3_controllers_omit_obsolete_descriptor_regtest_fallback() -> None:
    joinmarket_manifest = render_kubernetes_manifest(engine="joinmarket")
    joinmarket_job = next(item for item in joinmarket_manifest["items"] if item["kind"] == "Job")
    joinmarket_controller = next(
        container
        for container in joinmarket_job["spec"]["template"]["spec"]["containers"]
        if container["name"] == "controller"
    )
    assert "--joinmarket-descriptor-regtest-fallback" not in joinmarket_controller["command"][-1]

    wasabi_manifest = render_kubernetes_manifest(engine="wasabi")
    wasabi_job = next(item for item in wasabi_manifest["items"] if item["kind"] == "Job")
    wasabi_controller = next(
        container
        for container in wasabi_job["spec"]["template"]["spec"]["containers"]
        if container["name"] == "controller"
    )
    assert "--joinmarket-descriptor-regtest-fallback" not in wasabi_controller["command"][-1]


def test_s3_controller_receives_distributor_startup_timeout() -> None:
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
            distributor_startup_timeout="1800",
        )
    )
    job = next(item for item in manifest["items"] if item["kind"] == "Job")
    controller = next(
        container for container in job["spec"]["template"]["spec"]["containers"] if container["name"] == "controller"
    )

    environment = {item["name"]: item.get("value") for item in controller["env"]}
    assert environment["COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT"] == "1800"


def test_s3_controller_can_use_an_imported_btc_node_image() -> None:
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
            btc_node_image="btc-node:test",
            kubernetes_image_pull_policy="IfNotPresent",
            btc_node_initial_block_count="201",
        )
    )
    job = next(item for item in manifest["items"] if item["kind"] == "Job")
    controller = next(
        container for container in job["spec"]["template"]["spec"]["containers"] if container["name"] == "controller"
    )

    environment = {item["name"]: item.get("value") for item in controller["env"]}
    assert environment["BTC_NODE_IMAGE"] == "btc-node:test"
    assert environment["KUBERNETES_IMAGE_PULL_POLICY"] == "IfNotPresent"
    assert environment["COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT"] == "201"
    assert '--btc-node-image "$BTC_NODE_IMAGE"' in controller["command"][-1]


def test_combined_blocksci_and_report_jobs_run_the_stage_worker() -> None:
    blocksci = render_blocksci_s3_pbs(**COMMON, image="docker://blocksci", command="analyze")
    report = render_unified_report_s3_pbs(**COMMON, image="docker://pipeline", command="report")
    for script in (blocksci, report):
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        assert script.rstrip().endswith('exec python3 -s "$JOB/run.py" "$JOB/settings.json"')
        # Stale markers are cleared strictly on the frontend before qsub, never by
        # a PBS job that could race a newer submission.
        assert " rm " not in script
    assert "#PBS -l select=1:ncpus=8:mem=64gb:scratch_ssd=100gb" in blocksci
    assert "#PBS -l select=1:ncpus=2:mem=8gb:scratch_ssd=10gb" in report
    settings = json.loads(embedded_file(blocksci, "settings.json"))
    assert (settings["stage"], settings["job"], settings["command"]) == ("blocksci", "blocksci", "analyze")
    assert settings["include_report"] is True and settings["export_analysis"] is False
    assert (settings["ncpus"], settings["image_name"]) == (8, "docker://blocksci")
    assert settings["log_prefix"] == "s3://bucket/runs/run-1/logs"
    # Every stage uploads its whole output, so PBS need not copy the output file back.
    assert settings["job_log"] == "blocksci.pbs.log"
    assert settings_of(report)["job_log"] == "unified-report.pbs.log"


def test_reusable_blocksci_templates_archive_verify_and_avoid_reparse() -> None:
    parse = settings_of(
        render_blocksci_parse_s3_pbs(**COMMON, image="docker://blocksci", command=blocksci_parse_pbs_command("run-1"))
    )
    command = blocksci_analysis_pbs_command(
        "run-1",
        PipelineConfiguration.from_flat(
            {
                "coinjoin_type": "wasabi2",
                "min_input_count": 2,
                "joinmarket_detector": "definite",
                "joinmarket_min_base_fee": 5000,
                "joinmarket_percentage_fee": 4e-05,
                "joinmarket_max_depth": 200000,
            }
        ),
    )
    analyze = settings_of(render_blocksci_analyze_s3_pbs(**COMMON, image="docker://blocksci", command=command))

    assert (parse["stage"], parse["job"]) == ("blocksci-parse", "blocksci-parse")
    assert "/mnt/exporters/worker.py parse" in parse["command"]
    assert parse["source"]["kind"] == "emulator" and parse["persistent"] is None
    assert parse["job_log"] == "blocksci-parse.pbs.log"
    assert (analyze["stage"], analyze["job"]) == ("blocksci-analyze", "blocksci-analysis")
    assert analyze["job_log"] == "blocksci-analyze.pbs.log"
    assert "worker.py analyze" in analyze["command"] and "blocksci_parser" not in analyze["command"]
    assert analyze["inputs"] == [{"run": ".pipeline/exporters"}, {"run": "coinjoin_emulator_data"}]
    assert analyze["output_check"][0] == "blocksci-analysis_data/blocksci_analysis.json"
    assert analyze["source_run_id"] is None and analyze["persistent"] is None

    notebook = settings_of(
        render_blocksci_analyze_s3_pbs(
            **COMMON, image="docker://blocksci", command="uv run jupyter notebook", mode="blocksci-notebook"
        )
    )
    assert notebook["stage"] == "blocksci-notebook" and notebook["inputs"] == []
    assert "Open JupyterLab from Scenario Lab" in notebook["connection_help"][1]


def test_notebook_uses_image_python_and_keeps_control_files_outside_saved_notebooks() -> None:
    command = blocksci_notebook_pbs_command(8890)
    assert "/usr/bin/python3 /mnt/blocksci_notebook.py 8890" in command
    assert "uv run" not in command
    script = render_blocksci_analyze_s3_pbs(
        **COMMON, image="docker://blocksci", command=command, mode="blocksci-notebook", notebook_port=8890
    )
    settings = settings_of(script)
    assert settings["output"] == {"relative": "blocksci-notebooks_data", "notebook": True}
    notebook = embedded_file(script, "coinjoin_pipeline/execution/pbs/templates/blocksci_notebook.py")
    assert "--ServerApp.port_retries=0" in notebook
    assert "--ip=127.0.0.1" in notebook


def test_cache_compression_threads_come_from_rendered_ncpus() -> None:
    # MetaCentrum sets NCPUS and OMP_NUM_THREADS to 1 inside the job, so nproc reports 1 as well.
    parse = render_blocksci_parse_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command=blocksci_parse_pbs_command("run-1"),
        resources=replace(DEFAULT_RESOURCES["blocksci"], ncpus=7),
    )
    update = render_blocksci_update_s3_pbs(
        **COMMON,
        source_run_id="run-0",
        image="docker://blocksci",
        command=blocksci_update_pbs_command("run-1"),
        bitcoin_blocks_uri="s3://bucket/bitcoin-blocks/",
        expected_block_hash="a" * 64,
        external_network="bitcoin",
        external_max_block=850100,
        resources=replace(DEFAULT_RESOURCES["blocksci"], ncpus=5),
    )

    for script, ncpus in ((parse, 7), (update, 5)):
        assert f"select=1:ncpus={ncpus}:" in script
        assert settings_of(script)["ncpus"] == ncpus


def test_s3_parse_without_zstd_fails_through_the_exit_trap(tmp_path: Path) -> None:
    upload_order = tmp_path / "upload-order"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Only the tools the script needs before the zstd check and inside the trap.
    for tool in ("mkdir", "cat", "env", "find", "python3"):
        (fake_bin / tool).symlink_to(shutil.which(tool))
    fake_s5cmd = fake_bin / "s5cmd"
    fake_s5cmd.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, shutil, sys\n"
        "destination = sys.argv[-1]\n"
        "kind = 'marker' if destination.endswith('blocksci-parse.failed') else 'log'\n"
        "if kind == 'log':\n"
        "    shutil.copyfile(sys.argv[-2], os.environ['CAPTURED_LOG'])\n"
        "with pathlib.Path(os.environ['UPLOAD_ORDER']).open('a') as stream:\n"
        "    stream.write(kind + '\\n')\n",
        encoding="utf-8",
    )
    fake_s5cmd.chmod(0o755)
    credentials = tmp_path / "credentials"
    credentials.write_text("[coinjoin]\n", encoding="utf-8")
    captured_log = tmp_path / "uploaded.log"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = render_blocksci_parse_s3_pbs(
        S3Target(
            artifact_uri="s3://bucket/runs",
            run_id="run-1",
            endpoint_url="https://example.com",
            credentials_file=str(credentials),
            profile="coinjoin",
        ),
        image="docker://blocksci",
        command="true",
    )
    result = subprocess.run(
        [shutil.which("bash")],
        input=script,
        text=True,
        capture_output=True,
        env={
            "SCRATCHDIR": str(scratch),
            "PATH": str(fake_bin),
            "CAPTURED_LOG": str(captured_log),
            "UPLOAD_ORDER": str(upload_order),
        },
        check=False,
    )
    assert result.returncode == 1
    assert "zstd is required to pack or unpack the BlockSci cache" in captured_log.read_text(encoding="utf-8")
    assert upload_order.read_text(encoding="utf-8").splitlines() == ["log", "marker"]


def test_external_bitcoin_parse_uses_shared_blocks_without_s3_emulator_inputs() -> None:
    with (
        mock.patch("coinjoin_pipeline.execution.pbs.templates_s3.require_storage_path"),
        mock.patch("coinjoin_pipeline.execution.pbs.templates_s3.require_existing_path"),
        mock.patch.object(Path, "is_dir", return_value=True),
    ):
        script = render_blocksci_parse_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command=blocksci_parse_pbs_command(
                "run-1", coin_type="bitcoin", disk_path="/mnt/data", max_block_expression="850001"
            ),
            external_bitcoin_datadir=Path("/storage/external/bitcoin"),
            external_network="bitcoin",
            external_max_block=850000,
        )

    settings = settings_of(script)
    assert settings["source"] == {
        "kind": "external-bitcoin",
        "network": "bitcoin",
        "description": "external Bitcoin Core block directory",
        "datadir": "/storage/external/bitcoin",
        "max_block": 850000,
    }
    assert "--disk /mnt/data --max-block 850001" in settings["command"]
    assert "COINJOIN_FILE_BLOCK_ARCHIVE_PY" not in script


def test_s3_bitcoin_archive_parse_verifies_manifest_checksums_and_height() -> None:
    script = render_blocksci_parse_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command=blocksci_parse_pbs_command(
            "run-1", coin_type="bitcoin", disk_path="/mnt/data", max_block_expression="900001"
        ),
        bitcoin_blocks_uri="s3://bucket/bitcoin-mainnet/blocks",
        expected_block_hash="a" * 64,
        external_network="bitcoin",
        external_max_block=900000,
    )
    settings = settings_of(script)
    assert settings["source"]["kind"] == "bitcoin-blocks-s3"
    assert settings["source"]["blocks_uri"] == "s3://bucket/bitcoin-mainnet/blocks"
    assert settings["source"]["expected_hash"] == "a" * 64
    assert settings["python_command"] == BLOCKSCI_IMAGE_PYTHON_COMMAND
    # The archive helper that checks sidecar checksums ships with the job.
    assert "checksum mismatch" in embedded_file(script, "coinjoin_pipeline/execution/block_archive.py")


def test_s3_bitcoin_archive_parse_uploads_failure_log_before_marker(
    tmp_path: Path,
) -> None:
    captured_log = tmp_path / "uploaded.log"
    upload_order = tmp_path / "upload-order"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_s5cmd = fake_bin / "s5cmd"
    fake_s5cmd.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, shutil, sys\n"
        "source, destination = sys.argv[-2:]\n"
        "if destination.endswith('blocksci-parse.pbs.log'):\n"
        "    shutil.copyfile(source, os.environ['CAPTURED_LOG'])\n"
        "    kind = 'log'\n"
        "elif destination.endswith('blocksci-parse.failed'):\n"
        "    kind = 'marker'\n"
        "else:\n"
        "    raise SystemExit(f'unexpected upload: {destination}')\n"
        "with pathlib.Path(os.environ['UPLOAD_ORDER']).open('a') as stream:\n"
        "    stream.write(kind + '\\n')\n",
        encoding="utf-8",
    )
    fake_s5cmd.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = S3Target(
        artifact_uri="s3://bucket/runs",
        run_id="run-1",
        endpoint_url="https://example.com",
        credentials_file=str(tmp_path / "missing-credentials"),
        profile="coinjoin",
    )
    script = render_blocksci_parse_s3_pbs(
        target,
        image="docker://blocksci",
        command="true",
        bitcoin_blocks_uri="s3://bucket/bitcoin-blocks",
        expected_block_hash="a" * 64,
        external_network="bitcoin",
        external_max_block=1,
    )
    result = subprocess.run(
        ["bash"],
        input=script,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "SCRATCHDIR": str(scratch),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CAPTURED_LOG": str(captured_log),
            "UPLOAD_ORDER": str(upload_order),
        },
        check=False,
    )
    assert result.returncode == 1
    assert "S3 credentials file is not readable" in captured_log.read_text(encoding="utf-8")
    assert upload_order.read_text(encoding="utf-8").splitlines() == ["log", "marker"]
    assert list(scratch.iterdir()) == []


def test_cached_external_task_downloads_dumplings_baseline_and_uploads_report() -> None:
    command = blocksci_external_report_pbs_command(
        "run-1",
        PipelineConfiguration.from_flat(
            {
                "coinjoin_type": "joinmarket",
                "min_input_count": None,
                "joinmarket_detector": "definite",
                "joinmarket_min_base_fee": 5000,
                "joinmarket_percentage_fee": 4e-05,
                "joinmarket_max_depth": 200000,
            }
        ),
    )
    settings = settings_of(
        render_blocksci_analyze_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command=command,
            mode="blocksci-external",
            external_baseline_uri="s3://bucket/dumplings/coinjoin_tx_info.json",
        )
    )
    assert "--mode external --network bitcoin" in settings["command"]
    assert {
        "uri": "s3://bucket/dumplings/coinjoin_tx_info.json",
        "to": "coinjoin-analysis_data/coinjoin_tx_info.json",
    } in (settings["inputs"])
    assert settings["output"] == {"relative": "coinjoinPipeline_data"}
    assert settings["stage"] == "blocksci-external"


def test_incremental_blocksci_update_restores_source_and_publishes_fresh_target() -> None:
    with (
        mock.patch("coinjoin_pipeline.execution.pbs.templates_s3.require_storage_path"),
        mock.patch("coinjoin_pipeline.execution.pbs.templates_s3.require_existing_path"),
        mock.patch.object(Path, "is_dir", return_value=True),
    ):
        script = render_blocksci_update_s3_pbs(
            **COMMON,
            source_run_id="run-0",
            image="docker://blocksci",
            command=blocksci_update_pbs_command("run-1"),
            external_bitcoin_datadir=Path("/storage/external/bitcoin"),
            external_network="bitcoin",
            external_max_block=850100,
        )
    settings = settings_of(script)
    assert (settings["stage"], settings["source_run_id"]) == ("blocksci-update", "run-0")
    assert settings["artifact_uri"] == "s3://bucket/runs"
    assert settings["source"] == {
        "network": "bitcoin",
        "max_block": 850100,
        "kind": "external-bitcoin",
        "datadir": "/storage/external/bitcoin",
    }
    assert "worker.py update --run-dir /runs/emulation/logs/run-1" in settings["command"]
    assert "generate-config" not in settings["command"]


def test_incremental_blocksci_update_from_the_block_archive_downloads_only_new_files() -> None:
    script = render_blocksci_update_s3_pbs(
        **COMMON,
        source_run_id="run-0",
        image="docker://blocksci",
        command=blocksci_update_pbs_command("run-1"),
        bitcoin_blocks_uri="s3://bucket/bitcoin-blocks/",
        expected_block_hash="a" * 64,
        external_network="bitcoin",
        external_max_block=850100,
    )
    source = settings_of(script)["source"]
    assert source["kind"] == "bitcoin-blocks-s3" and source["blocks_uri"] == "s3://bucket/bitcoin-blocks"
    assert embedded_file(script, "coinjoin_pipeline/execution/block_archive.py")


@pytest.mark.parametrize(
    "sources", [{}, {"bitcoin_blocks_uri": "s3://bucket/blocks", "external_bitcoin_datadir": Path("/x")}]
)
def test_incremental_blocksci_update_needs_exactly_one_source(sources: dict[str, object]) -> None:
    with pytest.raises(PBSError, match="exactly one"):
        render_blocksci_update_s3_pbs(
            **COMMON,
            source_run_id="run-0",
            image="docker://blocksci",
            command="true",
            external_network="bitcoin",
            external_max_block=1,
            **sources,
        )


def test_external_blocksci_import_repackages_index_without_parser() -> None:
    with (
        mock.patch("coinjoin_pipeline.execution.pbs.templates_s3.require_storage_path"),
        mock.patch("coinjoin_pipeline.execution.pbs.templates_s3.require_existing_path"),
        mock.patch.object(Path, "is_file", return_value=True),
    ):
        script = render_blocksci_parse_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command=blocksci_parse_pbs_command("run-1"),
            external_blocksci_dir=Path("/storage/external/blocksci_data"),
        )
    source = settings_of(script)["source"]
    assert source["kind"] == "external-blocksci" and source["network"] == "from-config"
    assert source["blocksci_dir"] == "/storage/external/blocksci_data"


STUB_S5CMD = """#!/usr/bin/env bash
# Stand-in for s5cmd: the real binary is not installed on CI runners, and the
# preflight only depends on `ls` exit status plus the shape of its output.
target="${@: -1}"
case "$target" in
  */\\*)
    printf '%s' "$STUB_LISTING"
    exit "$STUB_LISTING_STATUS"
    ;;
  *)
    if [ "$STUB_REQUIRED_STATUS" != "0" ]; then
      echo "ERROR \\"ls $target\\": no object found" >&2
    fi
    exit "$STUB_REQUIRED_STATUS"
    ;;
esac
"""

STAGED_KEYS_LISTING = (
    "2026/07/26 04:00:00     1024  .pipeline/exporters/unified_report.py\n"
    "2026/07/26 04:00:00     2048  .pipeline/exporters/blocksci_export/analysis.py\n"
)
STAGED_DIR_LISTING = "                                  DIR  .pipeline/\n"


def run_prefix_preflight(
    listing: str, *, listing_status: int = 0, required_status: int = 0
) -> subprocess.CompletedProcess:
    manifest = render_kubernetes_manifest()
    job = next(item for item in manifest["items"] if item["kind"] == "Job")
    init_containers = job["spec"]["template"]["spec"]["initContainers"]
    script = next(container for container in init_containers if container["name"] == "prefix-preflight")["command"][-1]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        binaries = root / "bin"
        binaries.mkdir()
        stub = binaries / "s5cmd"
        stub.write_text(STUB_S5CMD, encoding="utf-8")
        stub.chmod(0o755)
        # The rendered script writes credentials to /credentials, which only
        # exists inside the pod; everything else about it runs unchanged.
        credentials = root / "credentials"
        script = script.replace("/credentials/credentials", str(credentials / "credentials"))
        script = script.replace("mkdir -p /credentials", f"mkdir -p {credentials}")
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{binaries}:{os.environ['PATH']}",
                "S3_ACCESS_KEY_ID": "key",
                "S3_SECRET_ACCESS_KEY": "secret",
                "S3_ENDPOINT_URL": "https://s3.cl4.du.cesnet.cz",
                "ARTIFACT_URI": "s3://bucket/runs",
                "RUN_ID": "run-1",
                "STUB_LISTING": listing,
                "STUB_LISTING_STATUS": str(listing_status),
                "STUB_REQUIRED_STATUS": str(required_status),
            },
        )


@pytest.mark.parametrize("listing", [STAGED_KEYS_LISTING, STAGED_DIR_LISTING])
def test_prefix_preflight_accepts_both_listing_shapes(listing: str) -> None:
    # Whether the wildcard expands across "/" (full keys) or collapses into a
    # DIR row decides whether every staged S3 run fails its own preflight.
    result = run_prefix_preflight(listing)
    assert result.returncode == 0, result.stderr


def test_prefix_preflight_rejects_a_reused_run_prefix() -> None:
    result = run_prefix_preflight(STAGED_KEYS_LISTING + "2026/07/26 04:00:00  512  .k8s/upload.done\n")
    assert result.returncode == 1
    assert "already contains artifacts" in result.stderr
    assert ".k8s/upload.done" in result.stderr


def test_prefix_preflight_rejects_incomplete_staging() -> None:
    result = run_prefix_preflight(
        'ERROR "ls s3://bucket/runs/run-1/*": no object found\n',
        listing_status=1,
        required_status=1,
    )
    assert result.returncode == 1
    assert "staged exporters are incomplete" in result.stderr


def exporter_staging_args() -> SimpleNamespace:
    return configuration(
        artifact_uri="s3://bucket/runs",
        run_id="run-9",
        s3_endpoint_url="https://s3.cl4.du.cesnet.cz",
        s3_credentials_file="/storage/user/.aws/credentials",
        s3_profile="coinjoin",
    )


def test_standalone_s3_emulation_can_supply_frontend_credentials() -> None:
    # stage_kubernetes_s3_run reads args.artifacts.credentials_file/s3_profile before
    # creating the Job. The emulate parser used to define neither, so a live
    # (non-dry) standalone S3 emulation died with AttributeError.
    arguments = [
        "emulate",
        "--engine",
        "wasabi",
        "--driver",
        "kubernetes",
        "--artifact-backend",
        "s3",
        "--artifact-uri",
        "s3://bucket/runs",
        "--s3-endpoint-url",
        "https://s3.example.invalid",
        "--s3-secret-name",
        "coinjoin-s3",
        "--run-id",
        "run-1",
        "--reuse-namespace",
        "--kubeconfig",
        "/dev/null",
    ]
    parser = build_parser()
    args = parser.parse_args(
        arguments
        + [
            "--s3-credentials-file",
            "/storage/user/.aws/credentials",
            "--s3-profile",
            "coinjoin",
        ]
    )
    assert args.artifacts.credentials_file == "/storage/user/.aws/credentials"
    assert args.artifacts.profile == "coinjoin"

    # And they are mandatory, not silently defaulted: the wrapper applies the
    # shared option rules to its argv before it acts on the parsed values.
    with pytest.raises(ValueError, match="s3-credentials-file"):
        resolve_configuration(parser.parse_args(arguments))


def test_s3_emulation_forwards_timeout_to_uploader_manifest(
    tmp_path: Path,
) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text('{"name": "private-experiment"}', encoding="utf-8")
    args = configuration(
        engine="wasabi",
        scenario=str(scenario),
        run_timezone="UTC",
        kubeconfig=None,
        dry_run=True,
        namespace="coinjoin",
        run_id="run-1",
        image_prefix="ghcr.io/ondrejman/",
        artifact_uri="s3://bucket/runs",
        s3_endpoint_url="https://s3.example.invalid",
        s3_secret_name="coinjoin-s3",
        emulation_timeout=123,
        reuse_namespace=True,
        uploader_image="uploader:test",
    )
    with (
        mock.patch(
            "coinjoin_pipeline.execution.s3_emulation.resolve_uploader_image",
            return_value="uploader:test",
        ),
        mock.patch(
            "coinjoin_pipeline.execution.s3_emulation.render_s3_emulation_resources",
            return_value="{}",
        ) as render,
    ):
        run_s3_kubernetes_emulation(args)

    assert render.call_args.kwargs["emulation_timeout_seconds"] == 123
    assert render.call_args.kwargs["scenario_json"] == scenario.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("python:3.12-slim-bookworm", "docker://python:3.12-slim-bookworm"),
        ("ghcr.io/ondrejman/x@sha256:abc", "docker://ghcr.io/ondrejman/x@sha256:abc"),
        ("docker://python:3.12", "docker://python:3.12"),
        # How the offline tests hand Apptainer a locally exported image; the
        # naive "://" test used to glue a second scheme in front of it.
        (
            "docker-archive:/storage/images/report.tar",
            "docker-archive:/storage/images/report.tar",
        ),
        ("oras://registry.example/x:1", "oras://registry.example/x:1"),
    ],
)
def test_unified_report_image_keeps_existing_uri_schemes(reference: str, expected: str) -> None:
    from coinjoin_pipeline.execution.pbs_settings import resolve_unified_report_image, with_singularity_scheme

    args = configuration(unified_report_image=reference)
    assert with_singularity_scheme(resolve_unified_report_image(args)) == expected


def test_frontend_rejects_an_s5cmd_without_exclude_support() -> None:
    from coinjoin_pipeline.storage.s3 import require_s5cmd_version

    with mock.patch("coinjoin_pipeline.storage.s3.s5cmd_version", return_value=(2, 0, 0)):
        with pytest.raises(ArtifactTransportError, match="too old"):
            require_s5cmd_version()
    # New enough, and an unparsable version must not block a run.
    with mock.patch("coinjoin_pipeline.storage.s3.s5cmd_version", return_value=(2, 3, 0)):
        require_s5cmd_version()
    with mock.patch("coinjoin_pipeline.storage.s3.s5cmd_version", return_value=None):
        require_s5cmd_version()


def kubectl_results(job_status: dict, pods: list[dict]):
    def fake_run(command, **_kwargs):
        payload = {"status": job_status} if "job" in command else {"items": pods}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    return fake_run


def waiting_pod(container_key: str, name: str, reason: str) -> dict:
    return {"status": {container_key: [{"name": name, "state": {"waiting": {"reason": reason}}}]}}


def test_job_probe_stops_waiting_when_a_container_cannot_start() -> None:
    # An unpullable uploader image leaves the Job active forever: the in-pod
    # watchdog runs inside that very image, so only the frontend can notice.
    from coinjoin_pipeline.execution.kubernetes import kubernetes_job_probe

    probe = kubernetes_job_probe(Path("/kube/config"), "coinjoin", "coinjoin-s3-run-1")
    for container_key, name in (
        ("initContainerStatuses", "prefix-preflight"),
        ("containerStatuses", "uploader"),
    ):
        with mock.patch(
            "coinjoin_pipeline.execution.kubernetes.subprocess.run",
            side_effect=kubectl_results({}, [waiting_pod(container_key, name, "ImagePullBackOff")]),
        ):
            assert probe() == "terminal", name


def test_job_probe_reports_queued_while_containers_are_pending() -> None:
    from coinjoin_pipeline.execution.kubernetes import kubernetes_job_probe

    probe = kubernetes_job_probe(Path("/kube/config"), "coinjoin", "coinjoin-s3-run-1")
    with mock.patch(
        "coinjoin_pipeline.execution.kubernetes.subprocess.run",
        side_effect=kubectl_results({}, [waiting_pod("containerStatuses", "controller", "PodInitializing")]),
    ):
        assert probe() == "queued"


def test_s3_emulation_job_name_is_unique_and_dns_safe() -> None:
    names = {s3_emulation_job_name(run_id) for run_id in ("test_1", "test.1", "Test-1")}
    assert len(names) == 3
    long_name = s3_emulation_job_name("x" * 80)
    assert len(long_name) <= 63
    assert not long_name.endswith("-")
    assert long_name == long_name.lower()


@pytest.mark.parametrize("include_report,export_analysis", [(False, False), (False, True), (True, False)])
def test_combined_blocksci_job_publishes_only_the_requested_outputs(include_report, export_analysis) -> None:
    script = render_blocksci_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command="parse",
        include_report=include_report,
        export_analysis=export_analysis,
    )

    settings = json.loads(embedded_file(script, "settings.json"))
    assert (settings["include_report"], settings["export_analysis"]) == (include_report, export_analysis)


def test_rollback_reports_jobs_it_could_not_cancel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rollback is best effort; an uncancelled job must not be reported as done.

    qdel can be missing entirely or refused by the scheduler, and those jobs keep
    consuming allocation, so the operator needs the exact recovery commands.
    """
    with mock.patch("coinjoin_pipeline.execution.s3_markers.qdel_pbs_job", side_effect=[False, True]) as qdel:
        rollback_s3_pbs_submissions([("coinjoin-analysis", "analysis.server"), ("blocksci", "blocksci.server")])

    assert qdel.call_args_list == [
        mock.call("blocksci.server"),
        mock.call("analysis.server"),
    ]
    stderr = capsys.readouterr().err
    assert "ROLLBACK INCOMPLETE" in stderr
    assert "blocksci: qdel blocksci.server" in stderr
    # The job that was cancelled must not appear in the recovery list.
    assert "coinjoin-analysis: qdel" not in stderr


def test_rollback_confirms_success_when_every_job_is_cancelled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with mock.patch("coinjoin_pipeline.execution.s3_markers.qdel_pbs_job", return_value=True):
        rollback_s3_pbs_submissions([("blocksci", "blocksci.server")])

    stderr = capsys.readouterr().err
    assert "ROLLBACK INCOMPLETE" not in stderr
    assert "Rolled back 1 submitted job(s)." in stderr


def test_acquire_lock_is_reentrant_for_the_same_process(tmp_path: Path) -> None:
    """full-run nests the PBS submit lock inside the general pipeline lock.

    flock keys locks to the open file description, so a second handle on a path
    this process already holds would block against our own first handle.
    """
    lock_path = tmp_path / ".pbs-submit.lock"
    first = acquire_lock(lock_path)
    second = acquire_lock(lock_path)
    assert first is second


@pytest.mark.parametrize("engine", ["wasabi", "joinmarket"])
def test_s3_controller_explicitly_runs_in_cluster(engine):
    manifest = render_kubernetes_manifest(engine=engine)
    job = next(item for item in manifest["items"] if item["kind"] == "Job")
    controller = next(
        container for container in job["spec"]["template"]["spec"]["containers"] if container["name"] == "controller"
    )
    command = shlex.split(controller["command"][-1])
    assert command.count("--in-cluster") == 1
    assert command.index("--in-cluster") < command.index("run")
    assert "--disable-port-forward" in command


def test_s3_controller_knows_its_own_pod_to_own_the_emulation_pods() -> None:
    manifest = render_kubernetes_manifest()
    job = next(item for item in manifest["items"] if item["kind"] == "Job")
    controller = next(
        container for container in job["spec"]["template"]["spec"]["containers"] if container["name"] == "controller"
    )
    field_paths = {
        item["name"]: item["valueFrom"]["fieldRef"]["fieldPath"] for item in controller["env"] if "valueFrom" in item
    }
    assert field_paths["COINJOIN_OWNER_POD_NAME"] == "metadata.name"
    assert field_paths["COINJOIN_OWNER_POD_UID"] == "metadata.uid"
    assert field_paths["COINJOIN_OWNER_POD_NAMESPACE"] == "metadata.namespace"


def test_kubernetes_manifest_has_controller_uploader_secret_and_rbac() -> None:
    manifest = render_kubernetes_manifest()
    kinds = {item["kind"] for item in manifest["items"]}
    assert {"ServiceAccount", "Role", "RoleBinding", "Job"}.issubset(kinds)
    assert "ClusterRole" not in kinds
    assert "ClusterRoleBinding" not in kinds
    rbac = [item for item in manifest["items"] if item["apiVersion"] == "rbac.authorization.k8s.io/v1"]
    assert {item["kind"] for item in rbac} == {"Role", "RoleBinding"}
    assert all(item["metadata"]["namespace"] == "coinjoin" for item in rbac)
    role_binding = next(item for item in rbac if item["kind"] == "RoleBinding")
    assert role_binding["roleRef"]["kind"] == "Role"
    role = next(item for item in rbac if item["kind"] == "Role")
    permissions = {resource: set(rule["verbs"]) for rule in role["rules"] for resource in rule["resources"]}
    assert permissions["pods/status"] == {"get"}
    assert {"get", "list", "watch"}.issubset(permissions["events"])
    assert "jobs" not in permissions

    job = next(item for item in manifest["items"] if item["kind"] == "Job")
    assert job["spec"]["ttlSecondsAfterFinished"] == 3600
    assert job["spec"]["activeDeadlineSeconds"] == 21600 + 1800 + 60
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
    subprocess.run(["bash", "-n"], input=prefix_preflight["command"][-1], text=True, check=True)
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

    assert any(mount["name"] == "credentials" for mount in containers["uploader"]["volumeMounts"])
    uploader_env = {item["name"]: item for item in containers["uploader"]["env"]}
    assert uploader_env["EMULATION_TIMEOUT_SECONDS"]["value"] == "21600"
    assert "JOB_NAME" not in uploader_env
    assert "controller exceeded emulation timeout" in containers["uploader"]["command"][-1]
    assert 'delete job "$JOB_NAME"' not in containers["uploader"]["command"][-1]
    rendered = json.dumps(manifest)
    assert "s5cmd" in rendered and "upload.done" in rendered and "upload.failed" in rendered
    assert "coinjoin-s3" in rendered
    assert "<access" not in rendered and "secret_key" not in rendered
    assert "POD_NAME" in rendered
    assert "metadata.name" in rendered
    assert "state.terminated.exitCode" in rendered
    assert "ImagePullBackOff" in rendered
    assert 's5 cp \\"/artifacts/$RUN_ID/.k8s/upload.failed\\"' in rendered


def test_apply_s3_resources_attaches_job_ownership_to_support_objects(
    tmp_path: Path,
) -> None:
    manifest = json.dumps(render_kubernetes_manifest())
    completed = subprocess.CompletedProcess([], 0, "", "")
    uid_result = subprocess.CompletedProcess([], 0, "job-uid-1", "")

    with mock.patch(
        "coinjoin_pipeline.execution.kubernetes.subprocess.run",
        side_effect=[
            completed,
            uid_result,
            *[completed for _ in S3_JOB_OWNED_RESOURCE_TYPES],
        ],
    ) as run:
        apply_s3_emulation_resources(manifest, tmp_path / "kubeconfig")

    patch_calls = run.call_args_list[2:]
    assert len(patch_calls) == len(S3_JOB_OWNED_RESOURCE_TYPES)
    assert [call.args[0][6] for call in patch_calls] == list(S3_JOB_OWNED_RESOURCE_TYPES)
    for call in patch_calls:
        command = call.args[0]
        owner_patch = json.loads(command[command.index("-p") + 1])
        owner = owner_patch["metadata"]["ownerReferences"][0]
        assert owner == {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": s3_emulation_job_name("run-1"),
            "uid": "job-uid-1",
            "controller": True,
            "blockOwnerDeletion": False,
        }


def test_apply_s3_resources_rolls_back_when_owner_patch_fails(
    tmp_path: Path,
) -> None:
    manifest = json.dumps(render_kubernetes_manifest())
    completed = subprocess.CompletedProcess([], 0, "", "")
    uid_result = subprocess.CompletedProcess([], 0, "job-uid-1", "")
    patch_failure = subprocess.CompletedProcess([], 1, "", "forbidden")

    with (
        mock.patch(
            "coinjoin_pipeline.execution.kubernetes.subprocess.run",
            side_effect=[completed, uid_result, patch_failure],
        ),
        mock.patch("coinjoin_pipeline.execution.kubernetes.delete_s3_emulation_job") as delete_job,
        mock.patch("coinjoin_pipeline.execution.kubernetes.delete_s3_emulation_support_resources") as delete_support,
        pytest.raises(RuntimeError, match="could not attach Job ownership"),
    ):
        apply_s3_emulation_resources(manifest, tmp_path / "kubeconfig")

    resource_name = s3_emulation_job_name("run-1")
    delete_job.assert_called_once_with(tmp_path / "kubeconfig", "coinjoin", resource_name)
    delete_support.assert_called_once_with(tmp_path / "kubeconfig", "coinjoin", resource_name)


def test_apply_s3_resources_rolls_back_when_initial_apply_fails(
    tmp_path: Path,
) -> None:
    """A rejected Job still leaves the earlier support resources applied.

    The manifest orders ServiceAccount/ConfigMap/Role/RoleBinding before the
    Job, so admission policy, a quota or an immutable existing Job can fail the
    apply after those four were accepted, with no Job to own them.
    """
    manifest = json.dumps(render_kubernetes_manifest())
    apply_failure = subprocess.CompletedProcess([], 1, "", "admission webhook denied")

    with (
        mock.patch(
            "coinjoin_pipeline.execution.kubernetes.subprocess.run",
            side_effect=[apply_failure],
        ),
        mock.patch("coinjoin_pipeline.execution.kubernetes.delete_s3_emulation_job") as delete_job,
        mock.patch("coinjoin_pipeline.execution.kubernetes.delete_s3_emulation_support_resources") as delete_support,
        pytest.raises(RuntimeError, match="kubectl apply failed with exit 1"),
    ):
        apply_s3_emulation_resources(manifest, tmp_path / "kubeconfig")

    resource_name = s3_emulation_job_name("run-1")
    delete_job.assert_called_once_with(tmp_path / "kubeconfig", "coinjoin", resource_name)
    delete_support.assert_called_once_with(tmp_path / "kubeconfig", "coinjoin", resource_name)


def test_kubernetes_manifest_reuses_existing_namespace() -> None:
    manifest = render_kubernetes_manifest(reuse_namespace=True)

    assert all(item["kind"] != "Namespace" for item in manifest["items"])
    assert all(item["metadata"].get("namespace") == "coinjoin" for item in manifest["items"])


def exporter_need_args(**overrides) -> SimpleNamespace:
    defaults = dict(
        analysisPbs=False,
        blocksciPbs=True,
        mappingsPbs=False,
        blocksci_workflow="reusable",
        blocksci_task="detect",
    )
    defaults.update(overrides)
    return configuration(**defaults)


def test_only_stages_that_run_the_exporters_require_them() -> None:
    # Baseline, mappings, parse, update, script and notebook jobs bind an empty
    # exporters directory at most; staging for them would let a local exporter
    # problem block a job that cannot use one.
    assert pbs_stages_need_exporters(exporter_need_args()) is True
    assert pbs_stages_need_exporters(exporter_need_args(blocksci_workflow="combined")) is True
    assert pbs_stages_need_exporters(exporter_need_args(blocksci_workflow="combined", blocksci_task="notebook")) is True
    assert pbs_stages_need_exporters(exporter_need_args(blocksci_task="parse"))
    assert pbs_stages_need_exporters(exporter_need_args(blocksci_workflow="cached", blocksci_task="update"))
    for overrides in (
        dict(blocksciPbs=False, analysisPbs=True),
        dict(blocksciPbs=False, mappingsPbs=True),
        dict(blocksci_task="script"),
        dict(blocksci_task="notebook"),
    ):
        assert pbs_stages_need_exporters(exporter_need_args(**overrides)) is False, overrides


def test_s3_pbs_scratch_type_selects_the_scratch_resource() -> None:
    script = render_coinjoin_analysis_s3_pbs(
        S3_TARGET, "docker://image", "echo hi", replace(ANALYSIS, scratch_type="local")
    )
    assert "#PBS -l select=1:ncpus=4:mem=16gb:scratch_local=50gb" in script
    with pytest.raises(PBSError, match="scratch type"):
        render_coinjoin_analysis_s3_pbs(S3_TARGET, "docker://image", "echo hi", replace(ANALYSIS, scratch_type="shm"))


def test_read_only_source_cache_feeds_an_analysis_of_another_run() -> None:
    command = blocksci_external_report_pbs_command(
        "run-1",
        PipelineConfiguration.from_flat(
            {
                "coinjoin_type": "wasabi2",
                "min_input_count": 15,
                "joinmarket_detector": "definite",
                "joinmarket_min_base_fee": 5000,
                "joinmarket_percentage_fee": 4e-05,
                "joinmarket_max_depth": 200000,
            }
        ),
    )
    settings = settings_of(
        render_blocksci_analyze_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command=command,
            mode="blocksci-external",
            source_run_id="mainnet-index",
            external_baseline_uri="s3://bucket/dumplings/coinjoin_tx_info.json",
        )
    )
    # The index comes from the source run; outputs, markers and provenance stay with this run.
    assert (settings["source_run_id"], settings["artifact_uri"]) == ("mainnet-index", "s3://bucket/runs")
    assert settings["run_prefix"] == "s3://bucket/runs/run-1"
    assert settings["output"] == {"relative": "coinjoinPipeline_data"}


@pytest.mark.parametrize(
    "mode,source,message",
    [
        ("blocksci-analyze", "mainnet-index", "cannot use a source cache"),
        ("blocksci-notebook", "run-1", "different run"),
    ],
)
def test_read_only_source_cache_rejects_detect_and_the_same_run(mode, source, message) -> None:
    with pytest.raises(PBSError, match=message):
        render_blocksci_analyze_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command="true",
            mode=mode,
            source_run_id=source,
        )


PERSISTENT = dict(
    persistent=PersistentIndex("/scratch/multijob-brno/user/blocksci-mainnet", "s3://bucket/blocksci-index"),
    multijob="brno",
)


def test_persistent_manifest_records_requested_cpu_and_memory(tmp_path) -> None:
    index_dir = tmp_path / "index 'with quotes"
    index_dir.mkdir()
    stage = Stage(
        {
            "stage": "blocksci-parse",
            "run_id": "parse-example",
            "run_prefix": "s3://bucket/runs/parse-example",
            "endpoint": "https://s3.test",
            "credentials": "/dev/null",
            "profile": "test",
            "image_name": "docker://blocksci",
            "source": {"kind": "bitcoin-blocks-s3", "network": "bitcoin"},
            "pbs_resources": {"ncpus": 3, "mem": "1536mb"},
        },
        tmp_path / "scratch",
    )
    stage.storage = mock.Mock()
    (stage.work / "blocksci_data").mkdir(parents=True)
    (stage.work / "blocksci_data/config.json").write_text("{}")
    index = IndexStore(str(index_dir), None)

    publish_persistent(stage, stage.settings, index, 42, {})

    manifest = json.loads((index_dir / "manifest.json").read_text())
    assert manifest["index_dir"] == str(index_dir)
    assert manifest["pbs_resources"] == {"ncpus": 3, "mem": "1536mb"}
    assert manifest["published_at"].endswith("Z")
    assert manifest["exported_max_block"] == 42
    stage.storage.copy.assert_called_once_with(
        index_dir / "manifest.json", "s3://bucket/runs/parse-example/blocksci-index-manifest.json"
    )


def test_persistent_update_works_in_place_under_the_writer_lock() -> None:
    with mock.patch("coinjoin_pipeline.execution.pbs.templates_s3.require_storage_path"):
        script = render_blocksci_update_s3_pbs(
            **COMMON,
            source_run_id=None,
            image="docker://blocksci",
            command=blocksci_update_pbs_command("run-1"),
            bitcoin_blocks_uri="s3://bucket/mainnet",
            expected_block_hash="a" * 64,
            external_network="bitcoin",
            external_max_block=850100,
            **PERSISTENT,
        )
    assert "scratch_ssd=" in script and ":multijob=brno\n" in script
    settings = settings_of(script)
    assert settings["source_run_id"] is None
    assert settings["persistent"] == {
        "index_dir": "/scratch/multijob-brno/user/blocksci-mainnet",
        "backup_uri": "s3://bucket/blocksci-index",
        "in_scratch": False,
        "restore": False,
        "dumplings_uri": None,
        "dumplings_dir": None,
        "storage_report_uri": None,
        "lock_seconds": 24 * 3600 + 600,
        "wait_seconds": 12 * 3600,
        "poll_seconds": 60,
    }


def test_persistent_parse_refuses_an_existing_index() -> None:
    with (
        mock.patch("coinjoin_pipeline.execution.pbs.templates_s3.require_storage_path"),
        mock.patch("coinjoin_pipeline.execution.pbs.templates_s3.require_existing_path"),
        mock.patch.object(Path, "is_dir", return_value=True),
    ):
        script = render_blocksci_parse_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command=blocksci_parse_pbs_command("run-1", coin_type="bitcoin", disk_path="/mnt/data"),
            external_bitcoin_datadir=Path("/storage/external/bitcoin"),
            external_network="bitcoin",
            external_max_block=850000,
            **PERSISTENT,
        )
    assert settings_of(script)["persistent"]["index_dir"] == "/scratch/multijob-brno/user/blocksci-mainnet"
    with pytest.raises(PBSError, match="persistent index is parsed from"):
        render_blocksci_parse_s3_pbs(**COMMON, image="docker://blocksci", command="parse", **PERSISTENT)


def test_persistent_analysis_reads_under_the_shared_lock() -> None:
    settings = settings_of(
        render_blocksci_analyze_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command="uv run jupyter notebook",
            mode="blocksci-notebook",
            **PERSISTENT,
        )
    )
    assert settings["persistent"]["index_dir"] == "/scratch/multijob-brno/user/blocksci-mainnet"
    assert settings["source_run_id"] is None


@pytest.mark.parametrize(
    "values,message",
    [
        (dict(mode="blocksci-analyze", **PERSISTENT), "cannot use a persistent index"),
        (dict(mode="blocksci-notebook", source_run_id="other", **PERSISTENT), "Choose one index source"),
        (dict(mode="blocksci-notebook", multijob="ostrava"), "multijob location"),
    ],
)
def test_persistent_analysis_rejects_invalid_combinations(values, message) -> None:
    with pytest.raises(PBSError, match=message):
        render_blocksci_analyze_s3_pbs(**COMMON, image="docker://blocksci", command="true", **values)


def test_persistent_index_paths_stay_on_shared_storage() -> None:
    with pytest.raises(PBSError, match="must be under"):
        PersistentIndex("/tmp/blocksci-mainnet")
    with pytest.raises(PBSError, match="Invalid index backup URI"):
        PersistentIndex("/scratch/multijob-brno/user/index", "/storage/brno2/home/user/copy")


def test_index_backup_job_reads_under_the_shared_lock() -> None:
    script = render_blocksci_index_backup_s3_pbs(
        **COMMON, **PERSISTENT, resources=replace(DEFAULT_RESOURCES["blocksci"], ncpus=16, walltime="12:00:00")
    )
    assert ":multijob=brno\n" in script
    settings = settings_of(script)
    assert (settings["stage"], settings["job"], settings["ncpus"]) == ("blocksci-backup", "blocksci-backup", 16)
    assert settings["persistent"]["lock_seconds"] == 12 * 3600 + 600
    assert "image" not in settings


def test_a_fixed_state_prefix_replaces_the_run_prefix_and_keeps_logs_per_run() -> None:
    target = replace(S3_TARGET, state_prefix="s3://bucket/bitcoin-mainnet/index")
    assert target.run_prefix == "s3://bucket/bitcoin-mainnet/index"
    assert target.log_prefix == "s3://bucket/bitcoin-mainnet/index/logs/run-1"
    assert (S3_TARGET.run_prefix, S3_TARGET.log_prefix) == ("s3://bucket/runs/run-1", "s3://bucket/runs/run-1/logs")
    settings = settings_of(render_blocksci_index_backup_s3_pbs(target=target, **PERSISTENT))
    assert settings["run_prefix"] == "s3://bucket/bitcoin-mainnet/index"
    assert settings["log_prefix"] == "s3://bucket/bitcoin-mainnet/index/logs/run-1"
    assert settings["run_id"] == "run-1"


def test_index_backup_job_needs_a_backup_prefix() -> None:
    with pytest.raises(PBSError, match="index-backup-uri"):
        render_blocksci_index_backup_s3_pbs(
            **COMMON, persistent=PersistentIndex("/scratch/multijob-brno/user/blocksci-mainnet")
        )


def test_external_report_downloads_the_curated_false_positives() -> None:
    settings = settings_of(
        render_blocksci_analyze_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command="true",
            mode="blocksci-external",
            external_baseline_uri="s3://bucket/dumplings/coinjoin_tx_info.json",
            external_false_cjtxs_uri="s3://bucket/dumplings/false_cjtxs.json",
        )
    )
    assert {"uri": "s3://bucket/dumplings/false_cjtxs.json", "to": "coinjoin-analysis_data/false_cjtxs.json"} in (
        settings["inputs"]
    )


def test_false_positives_belong_to_the_external_report() -> None:
    with pytest.raises(PBSError, match="external BlockSci report"):
        render_blocksci_analyze_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command="true",
            mode="blocksci-notebook",
            external_false_cjtxs_uri="s3://bucket/dumplings/false_cjtxs.json",
        )


def test_dumplings_baseline_reads_the_manifested_copy() -> None:
    script = render_coinjoin_analysis_dumplings_s3_pbs(
        **COMMON,
        image="docker://coinjoin-analysis",
        dumplings_uri="s3://bucket/dumplings",
        coinjoin_type="wasabi2",
    )

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    settings = json.loads(embedded_file(script, "settings.json"))
    assert (settings["stage"], settings["job"]) == ("coinjoin-analysis", "dumplings-baseline")
    assert settings["dumplings_uri"] == "s3://bucket/dumplings"
    assert (
        settings["command"]
        == "python -m cj_process.parse_dumplings --cjtype ww2 --action process_dumplings --target-path /work/dumplings"
    )
    assert (settings["mix_id"], settings["false_cjtxs"]) == ("wasabi2", "wasabi2")


def test_dumplings_baseline_covers_only_wasabi2_and_joinmarket() -> None:
    with pytest.raises(PBSError, match="wasabi2 and joinmarket"):
        render_coinjoin_analysis_dumplings_s3_pbs(
            **COMMON, image="docker://coinjoin-analysis", dumplings_uri="s3://bucket/dumplings", coinjoin_type="ww1"
        )


def test_external_report_fetches_the_snapshot_record_next_to_a_dumplings_baseline() -> None:
    settings = settings_of(
        render_blocksci_analyze_s3_pbs(
            **COMMON,
            image="docker://blocksci",
            command="true",
            mode="blocksci-external",
            external_baseline_uri="s3://bucket/runs/baseline-1/coinjoin-analysis_data/coinjoin_tx_info.json",
        )
    )
    assert {
        "uri": "s3://bucket/runs/baseline-1/coinjoin-analysis_data/dumplings-snapshot.json",
        "to": "coinjoin-analysis_data/dumplings-snapshot.json",
        "optional": True,
    } in settings["inputs"]
