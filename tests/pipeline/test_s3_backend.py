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
from coinjoin_pipeline.execution.pbs.templates_s3 import (
    _index_publish,
    PersistentIndex,
    render_blocksci_analyze_s3_pbs,
    render_blocksci_index_backup_s3_pbs,
    render_coinjoin_analysis_dumplings_s3_pbs,
    render_blocksci_parse_s3_pbs,
    render_blocksci_s3_pbs,
    render_blocksci_update_s3_pbs,
    render_coinjoin_analysis_s3_pbs,
    render_mappings_s3_pbs,
    render_unified_report_s3_pbs,
)
from coinjoin_pipeline.execution.pbs.defaults import DEFAULT_RESOURCES
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.s3_emulation import run_s3_kubernetes_emulation
from coinjoin_pipeline.execution.s3_markers import rollback_s3_pbs_submissions
from coinjoin_pipeline.execution.s3_staging import pbs_stages_need_exporters
from coinjoin_pipeline.storage.s3 import (  # noqa: E402
    ArtifactTransportError,
    S3Target,
)

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


def test_s3_pbs_templates_use_scratch_s5cmd_and_markers() -> None:
    coinjoin = render_coinjoin_analysis_s3_pbs(**COMMON, image="docker://coinjoin", command="analyze")
    blocksci = render_blocksci_s3_pbs(**COMMON, image="docker://blocksci", command="analyze")
    report = render_unified_report_s3_pbs(**COMMON, image="docker://pipeline", command="report")
    mappings = render_mappings_s3_pbs(
        **COMMON,
        enumerator_image="docker://enumerator",
        sake_image="docker://sake",
    )
    for script in (coinjoin, blocksci, mappings, report):
        assert 'RUNS_ROOT="$SCRATCHDIR/coinjoin-run"\nRUN_WORK="$RUNS_ROOT/$RUN_ID"' in script
        assert "s5cmd --credentials-file" in script
        assert '--profile "$S3_PROFILE"' in script
        assert '--endpoint-url "$S3_ENDPOINT_URL"' in script
        assert "/storage:/storage" not in script
        assert ".failed" in script and ".done" in script
        assert "aws s3" not in script and "s3cmd" not in script
        # The marker upload runs in the EXIT trap; a transient s5cmd failure
        # there must not abort the trap before the marker is written locally.
        assert "trap - EXIT TERM\n  set +e" in script
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    # Stale markers are cleared strictly on the frontend before qsub, never by
    # a PBS job that could race a newer submission.
    assert "STAGE=coinjoin-analysis\n" in coinjoin
    assert " rm " not in coinjoin
    assert "STAGE=blocksci\n" in blocksci
    assert "STAGE=unified-report\n" in report
    assert " rm " not in report
    assert '"$CONTAINER_WORK_ROOT:/runs/emulation/selected:rw"' in coinjoin
    assert '"$RUN_WORK/coinjoin-analysis_data:/runs/emulation/selected/$RUN_ID:rw"' in coinjoin
    assert '"$RUN_WORK/coinjoin_emulator_data/data:/runs/emulation/selected/$RUN_ID/data:ro"' in coinjoin
    assert '"$RUN_WORK:/runs/emulation/selected/$RUN_ID:rw"' not in coinjoin
    assert "did not produce coinjoin-analysis_data/coinjoin_tx_info.json" in coinjoin
    assert 'BITCOIN_DATADIR="$RUN_WORK/bitcoin_data"' in blocksci
    assert 'BITCOIN_DATADIR="$BITCOIN_DATADIR/data"' in blocksci
    assert '"$BITCOIN_DATADIR:/mnt/data:ro"' in blocksci
    assert "--cleanenv" in blocksci
    assert "--env VIRTUAL_ENV=" not in blocksci
    assert "--env PATH=" not in blocksci
    assert "--env PYTHONPATH=" not in blocksci
    assert "requires a Bitcoin datadir containing regtest/blocks" in blocksci
    assert "requires coinjoin-analysis_data/coinjoin_tx_info.json" in blocksci
    assert "Unified S3 report requires blocksci-analysis_data/blocksci_analysis.json" in report
    assert "Unified S3 report requires coinjoin-analysis_data/coinjoin_tx_info.json" in report
    assert "#PBS -l select=1:ncpus=8:mem=64gb:scratch_ssd=100gb" in blocksci
    assert "#PBS -l select=1:ncpus=2:mem=8gb:scratch_ssd=10gb" in report
    for script in (blocksci, report):
        assert 'REPORT_DIR="$RUN_WORK/coinjoinPipeline_data"' in script
        assert 'sync "$REPORT_DIR/" "$RUN_PREFIX/coinjoinPipeline_data/"' in script
        assert "blocksciEmulatorAnalysis_data" not in script
    assert "/mnt/data" not in report
    assert '"$RUN_PREFIX/*"' not in report
    assert '"$RUN_PREFIX/blocksci_data/*"' not in report
    assert '"$RUN_PREFIX/bitcoin_data/*"' not in report
    assert '"$RUN_PREFIX/blocksci-analysis_data/*"' in report
    assert '"$RUN_PREFIX/coinjoin-analysis_data/*"' in report
    assert '"$RUN_PREFIX/coinjoin_emulator_data/*"' in report
    assert '"$RUN_PREFIX/coinjoin-analysis_data/*"' in mappings
    assert '"$RUN_PREFIX/coinjoin-mappings_data/"' in mappings
    assert "STAGE=coinjoin-mappings\n" in mappings
    assert "coinjoin_mappings.json" in mappings


def test_reusable_blocksci_templates_archive_verify_and_avoid_reparse() -> None:
    parse = render_blocksci_parse_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command=blocksci_parse_pbs_command("run-1"),
    )
    analyze = render_blocksci_analyze_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command=blocksci_analysis_pbs_command(
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
        ),
    )

    subprocess.run(["bash", "-n"], input=parse, text=True, check=True)
    subprocess.run(["bash", "-n"], input=analyze, text=True, check=True)
    assert "/mnt/exporters/worker.py parse" in parse
    assert "blocksci_data.tar.zst" in parse
    assert 'sha256sum <"$fifo"' in parse
    assert '"schema_version": "1.1"' in parse
    assert "STAGE=blocksci-parse\n" in parse
    assert "blocksci_parser" not in analyze
    assert 'index_archive_restore "$RUN_PREFIX/blocksci-parse_data/blocksci_data.tar.zst"' in analyze
    assert analyze.index("command -v zstd") < analyze.index(
        's5 cp "$RUN_PREFIX/blocksci-parse_data/manifest.json"'
    )
    assert 'grep -Fq \'"schema_version": "1.1"\'' in analyze
    unpacked = analyze.index('index_archive_restore "$RUN_PREFIX/')
    assert unpacked < analyze.index('rm -f "$RUN_WORK/blocksci_data/.writing"') < analyze.index("singularity exec")
    assert "worker.py analyze" in analyze
    assert "--cleanenv" in analyze
    assert "--env VIRTUAL_ENV=" not in analyze
    assert "--env PATH=" not in analyze
    assert "--env PYTHONPATH=" not in analyze
    assert "STAGE=blocksci-analyze\n" in analyze
    assert '"$RUN_PREFIX/bitcoin_data/*"' not in analyze

    notebook = render_blocksci_analyze_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command="uv run jupyter notebook",
        mode="blocksci-notebook",
    )
    subprocess.run(["bash", "-n"], input=notebook, text=True, check=True)
    assert "Open JupyterLab from Scenario Lab" in notebook
    assert "STAGE=blocksci-notebook\n" in notebook
    assert '"$RUN_PREFIX/.pipeline/exporters/*"' not in notebook
    assert '"$RUN_PREFIX/coinjoin_emulator_data/*"' not in notebook


def test_notebook_uses_image_python_and_keeps_control_files_outside_saved_notebooks() -> None:
    command = blocksci_notebook_pbs_command(8890)
    assert "/usr/bin/python3 /mnt/blocksci_notebook.py 8890" in command
    assert "uv run" not in command
    script = render_blocksci_analyze_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command=command,
        mode="blocksci-notebook",
        notebook_port=8890,
    )
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert 'OUTPUT_DIR="${PBS_O_HOME:-$HOME}/coinjoin-notebooks/$RUN_ID"' in script
    assert 'NOTEBOOK_CONTROL="${PBS_O_HOME:-$HOME}/.local/share/coinjoin-pipeline/jupyter/$RUN_ID"' in script
    assert '--bind "$NOTEBOOK_CONTROL:/mnt/notebook-control:rw"' in script
    assert '--env "NOTEBOOK_JOB_ID=${PBS_JOBID:?}"' in script
    assert "--ServerApp.port_retries=0" in script
    assert "--ip=127.0.0.1" in script
    assert "blocksci_parser" not in script


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
        assert (
            f'index_archive_upload "$RUN_WORK" "$RUN_PREFIX/blocksci-parse_data/blocksci_data.tar.zst" '
            f'"{ncpus}" blocksci_data' in script
        )
        for variable in ("$NCPUS", "${NCPUS", "$OMP_NUM_THREADS", "nproc"):
            assert variable not in script


def test_s3_parse_without_zstd_fails_through_the_exit_trap(tmp_path: Path) -> None:
    upload_order = tmp_path / "upload-order"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Only the tools the script needs before the zstd check and inside the trap.
    for tool in ("mkdir", "env", "find", "python3"):
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
                "run-1",
                coin_type="bitcoin",
                disk_path="/mnt/data",
                max_block_expression="850001",
            ),
            external_bitcoin_datadir=Path("/storage/external/bitcoin"),
            external_network="bitcoin",
            external_max_block=850000,
        )

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert '--bind "$BITCOIN_DATADIR:/mnt/data:ro"' in script
    assert "--network bitcoin " in script
    assert "--disk /mnt/data --max-block 850001" in script
    assert '"$RUN_PREFIX/bitcoin_data/*"' not in script
    assert '"$RUN_PREFIX/coinjoin_emulator_data/' not in script
    assert '"external-bitcoin" "bitcoin" "$EXPORTED_MAX_BLOCK"' in script


def test_s3_bitcoin_archive_parse_verifies_manifest_checksums_and_height() -> None:
    script = render_blocksci_parse_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command=blocksci_parse_pbs_command(
            "run-1",
            coin_type="bitcoin",
            disk_path="/mnt/data",
            max_block_expression="900001",
        ),
        bitcoin_blocks_uri="s3://bucket/bitcoin-mainnet/blocks",
        expected_block_hash="a" * 64,
        external_network="bitcoin",
        external_max_block=900000,
    )

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert 'sync "$BITCOIN_BLOCKS_URI/*"' not in script
    assert 'cp "$BITCOIN_BLOCKS_URI/blk*.dat.json" "$ARCHIVE_WORK/sidecars/"' in script
    assert 'run "$ARCHIVE_WORK/download.s5cmd"' in script
    assert 'block_archive.py" verify "$BITCOIN_DATADIR/blocks" --max-block "$EXPORTED_MAX_BLOCK"\n' in script
    assert "checksum mismatch" in script
    assert "--source-manifest" not in script.split("<<'PY'")[0]
    assert '"bitcoin-blocks-s3" "bitcoin" "$EXPORTED_MAX_BLOCK" "$MANIFEST_EXTRA"' in script
    assert script.index("bash -c") < script.index("/mnt/exporters/verify_chain.py")
    assert script.index("/mnt/exporters/verify_chain.py") < script.index('index_archive_upload "$RUN_WORK"')
    assert "--expected-block-hash " + "a" * 64 in script


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
    script = render_blocksci_analyze_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command=command,
        mode="blocksci-external",
        external_baseline_uri="s3://bucket/dumplings/coinjoin_tx_info.json",
    )

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "--mode external --network bitcoin" in script
    assert "coinjoin_tx_info.json" in script
    assert 'OUTPUT_DIR="$RUN_WORK/coinjoinPipeline_data"' in script
    assert 's5 sync "$OUTPUT_DIR/" "$RUN_PREFIX/coinjoinPipeline_data/"' in script
    assert "STAGE=blocksci-external\n" in script


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

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "SOURCE_RUN_ID=run-0" in script
    assert 's5 cp "$ARTIFACT_URI/$SOURCE_RUN_ID/blocksci-parse_data/manifest.json"' in script
    assert '"$RUN_PREFIX/blocksci-parse_data/manifest.json"' in script
    unpacked = script.index('index_archive_restore "$ARTIFACT_URI/$SOURCE_RUN_ID/')
    assert script.index("command -v zstd") < unpacked
    assert unpacked < script.index('rm -f "$RUN_WORK/blocksci_data/.writing"')
    assert unpacked < script.index('index_archive_upload "$RUN_WORK"')
    assert '"source_kind": "external-bitcoin"' in script
    assert '"external-bitcoin" "$NETWORK" "$EXPORTED_MAX_BLOCK" "$MANIFEST_EXTRA"' in script
    assert '\\"cache_operation\\": \\"incremental-update\\"' in script
    assert '\\"source_run_id\\": \\"$SOURCE_RUN_ID\\"' in script
    assert "generate-config" not in script
    assert "worker.py update --run-dir /runs/emulation/logs/run-1" in script
    assert "Target maximum block" in script
    assert "STAGE=blocksci-update\n" in script


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

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "BITCOIN_BLOCKS_URI=s3://bucket/bitcoin-blocks\n" in script
    assert 'grep -Fq \'"source_kind": "bitcoin-blocks-s3"\'' in script
    assert '--source-manifest "$SOURCE_CACHE_DIR/manifest.json"' in script
    assert '"bitcoin-blocks-s3" "$NETWORK" "$EXPORTED_MAX_BLOCK" "$MANIFEST_EXTRA"' in script
    # The archive is read only after the source cache proved its maximum block.
    assert script.index("SOURCE_MAX_BLOCK=") < script.index('run "$ARCHIVE_WORK/download.s5cmd"')
    assert script.index('run "$ARCHIVE_WORK/download.s5cmd"') < script.index("singularity exec")
    assert script.index("worker.py update") < script.index("/mnt/exporters/verify_chain.py")
    assert script.index("/mnt/exporters/verify_chain.py") < script.index('index_archive_upload "$RUN_WORK"')
    assert '--source-manifest "/runs/emulation/logs/$RUN_ID/source-blocksci-parse_data/manifest.json"' in script


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

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert 'cp -a "$EXTERNAL_BLOCKSCI_DIR" "$RUN_WORK/blocksci_data"' in script
    assert "blocksci_parser" not in script
    assert '"external-blocksci" "from-config" "$EXPORTED_MAX_BLOCK"' in script
    assert "canonicalize_index\n" in script
    assert '"$RUN_PREFIX/bitcoin_data/*"' not in script


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


def test_blocksci_s3_analysis_mode_uploads_precomputed_artifact() -> None:
    blocksci = render_blocksci_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command="parse-and-analyze",
        include_report=False,
        export_analysis=True,
    )

    assert "blocksci-analysis_data/blocksci_analysis.json" in blocksci
    assert ('sync "$RUN_WORK/blocksci-analysis_data/" "$RUN_PREFIX/blocksci-analysis_data/"') in blocksci
    assert "coinjoinPipeline_data/" not in blocksci


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


def test_unified_report_downloads_mappings_only_when_requested() -> None:
    without_mappings = render_unified_report_s3_pbs(**COMMON, image="docker://pipeline", command="report")
    with_mappings = render_unified_report_s3_pbs(
        **COMMON,
        image="docker://pipeline",
        command="report",
        include_mappings=True,
    )

    assert '"$RUN_PREFIX/coinjoin-mappings_data/*"' not in without_mappings
    assert '"$RUN_PREFIX/coinjoin-mappings_data/*"' in with_mappings


def test_rendered_pbs_script_calls_fake_s5cmd_only_on_compute_path() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        bin_dir = root / "bin"
        scratch = root / "scratch"
        bin_dir.mkdir()
        scratch.mkdir()
        credentials = root / "credentials"
        credentials.write_text("[coinjoin]\naws_access_key_id=x\naws_secret_access_key=y\n")
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
            "    *coinjoin-analysis_data:/runs/emulation/selected/*:rw)\n"
            '      output_dir="${argument%%:*}"\n'
            '      printf \'{"coinjoins": {}}\\n\' > "$output_dir/coinjoin_tx_info.json"\n'
            "      ;;\n"
            "  esac\n"
            "done\n"
        )
        fake_singularity.chmod(0o700)
        script = render_coinjoin_analysis_s3_pbs(
            S3Target(
                artifact_uri="s3://bucket/runs",
                run_id="run-1",
                endpoint_url="https://s3.cl4.du.cesnet.cz",
                credentials_file=str(credentials),
                profile="coinjoin",
            ),
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
    script = render_blocksci_analyze_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command=command,
        mode="blocksci-external",
        source_run_id="mainnet-index",
        external_baseline_uri="s3://bucket/dumplings/coinjoin_tx_info.json",
    )

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "CACHE_RUN_ID=mainnet-index" in script
    assert 's5 cp "$ARTIFACT_URI/$CACHE_RUN_ID/blocksci-parse_data/manifest.json"' in script
    assert '"$RUN_PREFIX/blocksci-parse_data/*"' not in script
    assert 'grep -Fq "\\"run_id\\": \\"$CACHE_RUN_ID\\""' in script
    # Outputs, markers and provenance stay with the analysis run.
    assert '"$RUN_PREFIX/coinjoinPipeline_data/"' in script
    assert "STAGE=blocksci-external\n" in script
    provenance = script.index('cp "$CACHE_DIR/manifest.json" "$OUTPUT_DIR/blocksci-cache-manifest.json"')
    assert (
        script.index('index_archive_restore "$ARTIFACT_URI/$CACHE_RUN_ID/')
        < provenance
        < script.index("singularity exec")
    )


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
    index = tmp_path / "index 'with quotes"
    index.mkdir()
    resources = replace(DEFAULT_RESOURCES["blocksci"], ncpus=3, mem="1536mb")
    script = "\n".join(
        (
            "set -euo pipefail",
            f"RUN_WORK={shlex.quote(str(tmp_path))}",
            f"INDEX_DIR={shlex.quote(str(index))}",
            "RUN_ID=parse-example",
            "IMAGE=docker://blocksci",
            "IMAGE_NAME=docker://blocksci",
            "ARTIFACT_URI=s3://bucket/runs",
            "RUN_PREFIX=s3://bucket/runs/parse-example",
            "EXPORTED_MAX_BLOCK=42",
            "MANIFEST_EXTRA=''",
            'index_publish() { cp "$1" "$INDEX_DIR/manifest.json"; }',
            "index_unlock() { :; }",
            "s5() { :; }",
            _index_publish("blocksci-parse", "bitcoin-blocks-s3", "bitcoin", resources, str(index)),
        )
    )
    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)
    manifest = json.loads((index / "manifest.json").read_text())
    assert manifest["index_dir"] == str(index)
    assert manifest["pbs_resources"] == {"ncpus": 3, "mem": "1536mb"}
    assert manifest["published_at"].endswith("Z")
    assert manifest["exported_max_block"] == 42


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

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "scratch_ssd=" in script and ":multijob=brno\n" in script
    assert "blocksci_data.tar.zst" not in script
    calls = [script.index(f"\n{call}\n") for call in ("index_lock_exclusive", "index_reconcile")]
    assert calls[0] < calls[1] < script.index("worker.py update")
    assert "\nindex_bind rw\n" in script
    assert "INDEX_BACKUP=s3://bucket/blocksci-index" in script
    # The backup is its own job, never part of the update.
    assert "\nindex_backup\n" not in script
    assert 'index_publish "$RUN_WORK/index-manifest.json"' in script
    assert "index_unlock 2>/dev/null || :" in script
    assert '"$RUN_PREFIX/blocksci-index-manifest.json"' in script


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

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "use --blocksci-task update" in script
    lock = script.index("\nindex_lock_exclusive\n")
    assert (
        lock
        < script.index('rm -rf "$INDEX_DIR/parsed" "$INDEX_DIR/config.json" "$INDEX_DIR/files.txt"\n')
        < script.index("worker.py parse")
    )
    # The index stays in place: no S3 cache archive and no backup.
    assert "blocksci_data.tar.zst" not in script
    assert "\nindex_backup\n" not in script


def test_persistent_analysis_reads_under_the_shared_lock() -> None:
    script = render_blocksci_analyze_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command="uv run jupyter notebook",
        mode="blocksci-notebook",
        **PERSISTENT,
    )

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "\nindex_lock_shared\n" in script and "\nindex_lock_exclusive\n" not in script
    assert "\nindex_bind ro\n" in script
    assert '"${INDEX_BINDS[@]}"' in script
    assert "blocksci-parse_data/*" not in script
    assert 'cp "$INDEX_DIR/manifest.json" "$OUTPUT_DIR/blocksci-index-manifest.json"' in script


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

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert ":multijob=brno\n" in script
    calls = [script.index(f"\n{call}\n") for call in ("index_lock_shared", "index_backup", "index_unlock")]
    assert calls == sorted(calls)
    assert "\nindex_lock_exclusive\n" not in script
    assert "INDEX_ZSTD_THREADS=16" in script
    assert '"$RUN_PREFIX/blocksci-index-backup.json"' in script
    assert "STAGE=blocksci-backup\n" in script


def test_a_fixed_state_prefix_replaces_the_run_prefix_and_keeps_logs_per_run() -> None:
    target = replace(S3_TARGET, state_prefix="s3://bucket/bitcoin-mainnet/index")
    assert target.run_prefix == "s3://bucket/bitcoin-mainnet/index"
    assert target.log_prefix == "s3://bucket/bitcoin-mainnet/index/logs/run-1"
    assert (S3_TARGET.run_prefix, S3_TARGET.log_prefix) == ("s3://bucket/runs/run-1", "s3://bucket/runs/run-1/logs")
    script = render_blocksci_index_backup_s3_pbs(target=target, **PERSISTENT)
    assert "RUN_PREFIX=s3://bucket/bitcoin-mainnet/index\n" in script
    assert "LOG_PREFIX=s3://bucket/bitcoin-mainnet/index/logs/run-1\n" in script
    assert "RUN_ID=run-1\n" in script
    assert '"$RUN_PREFIX/.pbs/$STAGE.done"' in script


def test_index_backup_job_needs_a_backup_prefix() -> None:
    with pytest.raises(PBSError, match="index-backup-uri"):
        render_blocksci_index_backup_s3_pbs(
            **COMMON, persistent=PersistentIndex("/scratch/multijob-brno/user/blocksci-mainnet")
        )


def test_external_report_downloads_the_curated_false_positives() -> None:
    script = render_blocksci_analyze_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command="true",
        mode="blocksci-external",
        external_baseline_uri="s3://bucket/dumplings/coinjoin_tx_info.json",
        external_false_cjtxs_uri="s3://bucket/dumplings/false_cjtxs.json",
    )

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert '"$RUN_WORK/coinjoin-analysis_data/false_cjtxs.json"' in script
    assert "s3://bucket/dumplings/false_cjtxs.json" in script


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
    assert '"$DUMPLINGS_URI/manifest.json"' in script
    assert '"$DUMPLINGS_URI/Scanner/*"' in script
    assert "does not match its manifest" in script
    assert "--cjtype ww2 --action process_dumplings --target-path /work/dumplings" in script
    assert "MIX_ID=wasabi2" in script
    # A stale coinjoin-analysis cache next to the Dumplings files would replace them.
    assert "-name '*.txt.json' -delete" in script
    assert script.index("-name '*.txt.json' -delete") < script.index("cj_process.parse_dumplings")
    assert '"$RUN_PREFIX/coinjoin-analysis_data/"' in script
    assert "STAGE=coinjoin-analysis\n" in script


def test_dumplings_baseline_covers_only_wasabi2_and_joinmarket() -> None:
    with pytest.raises(PBSError, match="wasabi2 and joinmarket"):
        render_coinjoin_analysis_dumplings_s3_pbs(
            **COMMON, image="docker://coinjoin-analysis", dumplings_uri="s3://bucket/dumplings", coinjoin_type="ww1"
        )


def test_external_report_fetches_the_snapshot_record_next_to_a_dumplings_baseline() -> None:
    script = render_blocksci_analyze_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command="true",
        mode="blocksci-external",
        external_baseline_uri="s3://bucket/runs/baseline-1/coinjoin-analysis_data/coinjoin_tx_info.json",
    )

    assert "s3://bucket/runs/baseline-1/coinjoin-analysis_data/dumplings-snapshot.json" in script
    assert "the baseline has no dumplings-snapshot.json" in script
