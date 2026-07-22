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

from client.kubernetes import (  # noqa: E402
    render_s3_emulation_resources,
    s3_emulation_job_name,
)
from client.pbs import (  # noqa: E402
    blocksci_analysis_pbs_command,
    blocksci_export_pbs_command,
    blocksci_parse_pbs_command,
    blocksci_update_pbs_command,
    render_blocksci_analyze_s3_pbs,
    render_blocksci_parse_s3_pbs,
    render_blocksci_update_s3_pbs,
    render_blocksci_s3_pbs,
    render_coinjoin_analysis_s3_pbs,
    render_mappings_s3_pbs,
    render_unified_report_s3_pbs,
    submit_blocksci_s3_pbs,
    submit_coinjoin_analysis_s3_pbs,
    submit_mappings_s3_pbs,
    submit_unified_report_s3_pbs,
)
from client.wrapper import (  # noqa: E402
    build_parser,
    run_pbs_from_s3,
    validate_artifact_arguments,
)

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


def s3_pbs_args(
    *, analysis: bool = True, blocksci: bool = True, mappings: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_uri=COMMON["artifact_uri"],
        run_id=COMMON["run_id"],
        s3_endpoint_url=COMMON["endpoint_url"],
        s3_credentials_file=COMMON["credentials_file"],
        s3_profile=COMMON["profile"],
        dry_run=False,
        analysisPbs=analysis,
        blocksciPbs=blocksci,
        mappingsPbs=mappings,
        coinjoin_type="wasabi2",
        min_input_count=2,
        joinmarket_detector="definite",
        joinmarket_min_base_fee=5000,
        joinmarket_percentage_fee=0.00004,
        joinmarket_max_depth=200000,
        test_values=True,
        blocksci_workflow="combined",
        blocksci_task="detect",
        blocksci_script=None,
        blocksci_notebook_port=8888,
        blocksci_notebooks_dir=None,
        blocksci_cache_source_run_id=None,
        blocksci_external_bitcoin_datadir=None,
        blocksci_external_blocksci_dir=None,
        blocksci_network=None,
        blocksci_max_block=None,
    )


def test_s3_pbs_templates_use_scratch_s5cmd_and_markers() -> None:
    coinjoin = render_coinjoin_analysis_s3_pbs(
        **COMMON, image="docker://coinjoin", command="analyze"
    )
    blocksci = render_blocksci_s3_pbs(
        **COMMON, image="docker://blocksci", command="analyze"
    )
    report = render_unified_report_s3_pbs(
        **COMMON, image="docker://pipeline", command="report"
    )
    mappings = render_mappings_s3_pbs(
        **COMMON,
        enumerator_image="docker://enumerator",
        sake_image="docker://sake",
    )
    for script in (coinjoin, blocksci, mappings, report):
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
    assert "Unified S3 report requires blocksci-analysis_data/blocksci_analysis.json" in report
    assert "Unified S3 report requires coinjoin-analysis_data/coinjoin_tx_info.json" in report
    assert "#PBS -l select=1:ncpus=8:mem=64gb:scratch_local=100gb" in blocksci
    assert "#PBS -l select=1:ncpus=2:mem=8gb:scratch_local=10gb" in report
    for script in (blocksci, report):
        assert 'REPORT_DIR="$RUN_WORK/coinjoinPipeline_data"' in script
        assert 'sync "$REPORT_DIR/" "$ARTIFACT_URI/$RUN_ID/coinjoinPipeline_data/"' in script
        assert "blocksciEmulatorAnalysis_data" not in script
    assert "/mnt/data" not in report
    assert '"$ARTIFACT_URI/$RUN_ID/*"' not in report
    assert '"$ARTIFACT_URI/$RUN_ID/blocksci_data/*"' not in report
    assert '"$ARTIFACT_URI/$RUN_ID/bitcoin_data/*"' not in report
    assert '"$ARTIFACT_URI/$RUN_ID/blocksci-analysis_data/*"' in report
    assert '"$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/*"' in report
    assert '"$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/*"' in report
    assert '"$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/*"' in mappings
    assert '"$ARTIFACT_URI/$RUN_ID/coinjoin-mappings_data/"' in mappings
    assert ".pbs/coinjoin-mappings.done" in mappings
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
            "run-1", "wasabi2", 2, "definite", 5000, 0.00004, 200000, True
        ),
    )

    subprocess.run(["bash", "-n"], input=parse, text=True, check=True)
    subprocess.run(["bash", "-n"], input=analyze, text=True, check=True)
    assert "blocksci_parser" in parse
    assert "blocksci_data.tar.gz" in parse
    assert "sha256sum blocksci_data.tar.gz" in parse
    assert ".pbs/blocksci-parse.done" in parse
    assert "blocksci_parser" not in analyze
    assert "sha256sum -c blocksci_data.tar.gz.sha256" in analyze
    assert "blocksci_analysis.py" in analyze
    assert ".pbs/blocksci-analyze.done" in analyze
    assert '"$ARTIFACT_URI/$RUN_ID/bitcoin_data/*"' not in analyze

    notebook = render_blocksci_analyze_s3_pbs(
        **COMMON,
        image="docker://blocksci",
        command="uv run jupyter notebook",
        mode="blocksci-notebook",
    )
    subprocess.run(["bash", "-n"], input=notebook, text=True, check=True)
    assert "ssh -N -J $LOGIN@$FRONTEND" in notebook
    assert ".pbs/blocksci-notebook.done" in notebook
    assert '"$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*"' not in notebook
    assert '"$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/*"' not in notebook


def test_external_bitcoin_parse_uses_shared_blocks_without_s3_emulator_inputs() -> None:
    with (
        mock.patch("client.pbs.require_storage_path"),
        mock.patch("client.pbs.require_existing_path"),
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
    assert "generate-config bitcoin " in script
    assert "--disk /mnt/data --max-block 850001" in script
    assert '"$ARTIFACT_URI/$RUN_ID/bitcoin_data/*"' not in script
    assert '"$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/' not in script
    assert '"external-bitcoin" "bitcoin" "$EXPORTED_MAX_BLOCK"' in script


def test_incremental_blocksci_update_restores_source_and_publishes_fresh_target() -> None:
    with (
        mock.patch("client.pbs.require_storage_path"),
        mock.patch("client.pbs.require_existing_path"),
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
    assert 'SOURCE_RUN_ID=run-0' in script
    assert '"$ARTIFACT_URI/$SOURCE_RUN_ID/blocksci-parse_data/*"' in script
    assert '"$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/"' in script
    assert "sha256sum -c blocksci_data.tar.gz.sha256" in script
    assert '"source_kind": "external-bitcoin"' in script
    assert '"cache_operation": "incremental-update"' in script
    assert '"source_run_id": "%s"' in script
    assert "generate-config" not in script
    assert "blocksci_parser /runs/emulation/logs/run-1/blocksci_data/config.json update" in script
    assert "Target maximum block" in script
    assert ".pbs/blocksci-update.done" in script


def test_external_blocksci_import_repackages_index_without_parser() -> None:
    with (
        mock.patch("client.pbs.require_storage_path"),
        mock.patch("client.pbs.require_existing_path"),
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
    assert "CANONICAL_PARSED" in script
    assert '"$ARTIFACT_URI/$RUN_ID/bitcoin_data/*"' not in script


def test_wrapper_images_package_unified_report_s3_template() -> None:
    for dockerfile in (
        PROJECT_ROOT / "Dockerfile",
        PROJECT_ROOT / "pipeline" / "client" / "Dockerfile",
    ):
        content = dockerfile.read_text(encoding="utf-8")
        assert "unified_report_s3_template.sh" in content
        assert "blocksci_parse_s3_template.sh" in content
        assert "blocksci_update_s3_template.sh" in content
        assert "blocksci_analyze_s3_template.sh" in content
        assert "mappings_s3_template.sh" in content


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
    assert (
        'sync "$RUN_WORK/blocksci-analysis_data/" '
        '"$ARTIFACT_URI/$RUN_ID/blocksci-analysis_data/"'
    ) in blocksci
    assert "coinjoinPipeline_data/" not in blocksci


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


def test_mappings_submission_forwards_analysis_dependency() -> None:
    with (
        mock.patch("client.pbs.require_qsub"),
        mock.patch("client.pbs.submit_pbs_text", return_value="mappings.server") as qsub,
    ):
        assert (
            submit_mappings_s3_pbs(
                **COMMON,
                enumerator_image="docker://enumerator",
                sake_image="docker://sake",
                dependency_job_id="analysis.server",
            )
            == "mappings.server"
        )
    assert qsub.call_args.args[1] == "analysis.server"


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
    assert blocksci.call_args.kwargs["export_analysis"] is True
    assert "unified_report.py" not in blocksci.call_args.kwargs["command"]
    assert "blocksci_analysis.py" in blocksci.call_args.kwargs["command"]
    assert report.call_args.kwargs["dependency_job_ids"] == (
        "analysis.server",
        "blocksci.server",
    )
    assert report.call_args.kwargs["ncpus"] == 2
    assert report.call_args.kwargs["mem"] == "8gb"
    assert report.call_args.kwargs["scratch"] == "10gb"
    assert report.call_args.kwargs["walltime"] == "01:00:00"
    assert report.call_args.kwargs["image"] == (
        "docker://ghcr.io/ondrejman/coinjoin-pipeline:latest"
    )
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


def test_pbs_from_s3_mappings_depend_on_analysis_and_gate_report() -> None:
    args = s3_pbs_args(mappings=True)
    with (
        mock.patch(
            "client.wrapper.submit_coinjoin_analysis_s3_pbs",
            return_value="analysis.server",
        ),
        mock.patch(
            "client.wrapper.submit_mappings_s3_pbs",
            return_value="mappings.server",
        ) as mappings,
        mock.patch(
            "client.wrapper.submit_blocksci_s3_pbs",
            return_value="blocksci.server",
        ) as blocksci,
        mock.patch(
            "client.wrapper.submit_unified_report_s3_pbs",
            return_value="report.server",
        ) as report,
    ):
        jobs = run_pbs_from_s3(args)

    assert mappings.call_args.kwargs["dependency_job_id"] == "analysis.server"
    assert blocksci.call_args.kwargs["include_report"] is False
    assert blocksci.call_args.kwargs["export_analysis"] is True
    assert report.call_args.kwargs["dependency_job_ids"] == (
        "analysis.server",
        "blocksci.server",
        "mappings.server",
    )
    assert report.call_args.kwargs["include_mappings"] is True
    assert jobs.coinjoin_mappings == "mappings.server"


def test_pbs_from_s3_mappings_only_uses_existing_baseline() -> None:
    args = s3_pbs_args(analysis=False, blocksci=False, mappings=True)
    with (
        mock.patch(
            "client.wrapper.submit_mappings_s3_pbs",
            return_value="mappings.server",
        ) as mappings,
        mock.patch("client.wrapper.submit_unified_report_s3_pbs") as report,
    ):
        jobs = run_pbs_from_s3(args)

    mappings.assert_called_once()
    assert mappings.call_args.kwargs["dependency_job_id"] is None
    report.assert_not_called()
    assert jobs.coinjoin_mappings == "mappings.server"


def test_unified_report_downloads_mappings_only_when_requested() -> None:
    without_mappings = render_unified_report_s3_pbs(
        **COMMON, image="docker://pipeline", command="report"
    )
    with_mappings = render_unified_report_s3_pbs(
        **COMMON,
        image="docker://pipeline",
        command="report",
        include_mappings=True,
    )

    assert '"$ARTIFACT_URI/$RUN_ID/coinjoin-mappings_data/*"' not in without_mappings
    assert '"$ARTIFACT_URI/$RUN_ID/coinjoin-mappings_data/*"' in with_mappings


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
    assert blocksci.call_args.kwargs["export_analysis"] is False
    assert "unified_report.py" in blocksci.call_args.kwargs["command"]


def test_pbs_from_s3_reusable_submits_parse_analyze_and_report_chain() -> None:
    args = s3_pbs_args()
    args.blocksci_workflow = "reusable"
    with (
        mock.patch(
            "client.wrapper.submit_coinjoin_analysis_s3_pbs",
            return_value="analysis.server",
        ),
        mock.patch(
            "client.wrapper.submit_blocksci_parse_s3_pbs",
            return_value="parse.server",
        ) as parse,
        mock.patch(
            "client.wrapper.submit_blocksci_analyze_s3_pbs",
            return_value="blocksci-analyze.server",
        ) as analyze,
        mock.patch(
            "client.wrapper.submit_unified_report_s3_pbs",
            return_value="report.server",
        ) as report,
    ):
        jobs = run_pbs_from_s3(args)

    parse.assert_called_once()
    analyze.assert_called_once()
    assert analyze.call_args.kwargs["dependency_job_id"] == "parse.server"
    assert analyze.call_args.kwargs["mode"] == "blocksci-analyze"
    assert "blocksci_parser" not in analyze.call_args.kwargs["command"]
    assert report.call_args.kwargs["dependency_job_ids"] == (
        "analysis.server",
        "blocksci-analyze.server",
    )
    assert jobs.blocksci_parse == "parse.server"
    assert jobs.blocksci_work == "blocksci-analyze.server"


def test_pbs_from_s3_cached_notebook_skips_parse_and_report() -> None:
    args = s3_pbs_args(analysis=False)
    args.blocksci_workflow = "cached"
    args.blocksci_task = "notebook"
    with (
        mock.patch("client.wrapper.submit_blocksci_parse_s3_pbs") as parse,
        mock.patch(
            "client.wrapper.submit_blocksci_analyze_s3_pbs",
            return_value="notebook.server",
        ) as notebook,
        mock.patch("client.wrapper.submit_unified_report_s3_pbs") as report,
    ):
        jobs = run_pbs_from_s3(args)

    parse.assert_not_called()
    report.assert_not_called()
    assert notebook.call_args.kwargs["dependency_job_id"] is None
    assert notebook.call_args.kwargs["mode"] == "blocksci-notebook"
    assert "jupyter notebook" in notebook.call_args.kwargs["command"]
    assert jobs.blocksci_work == "notebook.server"


def test_pbs_from_s3_parse_only_publishes_cache_without_work() -> None:
    args = s3_pbs_args(analysis=False)
    args.blocksci_workflow = "reusable"
    args.blocksci_task = "parse"
    with (
        mock.patch(
            "client.wrapper.submit_blocksci_parse_s3_pbs",
            return_value="parse.server",
        ) as parse,
        mock.patch("client.wrapper.submit_blocksci_analyze_s3_pbs") as work,
        mock.patch("client.wrapper.submit_unified_report_s3_pbs") as report,
    ):
        jobs = run_pbs_from_s3(args)

    parse.assert_called_once()
    work.assert_not_called()
    report.assert_not_called()
    assert jobs.blocksci_parse == "parse.server"
    assert jobs.blocksci_work is None


def test_pbs_from_s3_incremental_update_preflights_and_submits_only_update() -> None:
    args = s3_pbs_args(analysis=False)
    args.run_id = "run-2"
    args.blocksci_workflow = "cached"
    args.blocksci_task = "update"
    args.blocksci_cache_source_run_id = "run-1"
    args.blocksci_external_bitcoin_datadir = "/storage/external/bitcoin"
    args.blocksci_network = "bitcoin"
    args.blocksci_max_block = 850100
    with (
        mock.patch("client.wrapper.s3_access_preflight") as preflight,
        mock.patch("client.wrapper.s3_object_exists", return_value=True) as exists,
        mock.patch("client.wrapper.ensure_empty_run_prefix") as empty,
        mock.patch(
            "client.wrapper.submit_blocksci_update_s3_pbs",
            return_value="update.server",
        ) as update,
        mock.patch("client.wrapper.submit_blocksci_parse_s3_pbs") as parse,
        mock.patch("client.wrapper.submit_blocksci_analyze_s3_pbs") as work,
        mock.patch("client.wrapper.submit_unified_report_s3_pbs") as report,
    ):
        jobs = run_pbs_from_s3(args)

    preflight.assert_called_once()
    assert "run-1/blocksci-parse_data/manifest.json" in exists.call_args.args[1]
    empty.assert_called_once()
    update.assert_called_once()
    kwargs = update.call_args.kwargs
    assert kwargs["source_run_id"] == "run-1"
    assert kwargs["external_bitcoin_datadir"] == Path("/storage/external/bitcoin")
    assert kwargs["external_max_block"] == 850100
    assert "generate-config" not in kwargs["command"]
    parse.assert_not_called()
    work.assert_not_called()
    report.assert_not_called()
    assert jobs.blocksci_update == "update.server"


def test_wrapper_accepts_versioned_incremental_update_arguments() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "pbs-from-s3",
            "--run-id", "run-2",
            "--artifact-uri", "s3://bucket/runs",
            "--s3-endpoint-url", "https://s3.cl4.du.cesnet.cz",
            "--s3-credentials-file", "/storage/user/.aws/credentials",
            "--s3-profile", "coinjoin",
            "--engine", "joinmarket",
            "--blocksciPbs",
            "--blocksci-workflow", "cached",
            "--blocksci-task", "update",
            "--blocksci-cache-source-run-id", "run-1",
            "--blocksci-external-bitcoin-datadir", "/storage/external/bitcoin",
            "--blocksci-network", "bitcoin",
            "--blocksci-max-block", "850100",
        ]
    )

    validate_artifact_arguments(parser, args)
    assert args.blocksci_cache_source_run_id == "run-1"
    assert args.run_id == "run-2"


def test_pbs_from_s3_external_bitcoin_builds_network_specific_parse() -> None:
    args = s3_pbs_args(analysis=False)
    args.blocksci_workflow = "reusable"
    args.blocksci_task = "parse"
    args.blocksci_external_bitcoin_datadir = "/storage/external/bitcoin"
    args.blocksci_external_blocksci_dir = None
    args.blocksci_network = "bitcoin"
    args.blocksci_max_block = 850000
    with mock.patch(
        "client.wrapper.submit_blocksci_parse_s3_pbs", return_value="parse.server"
    ) as parse:
        run_pbs_from_s3(args)

    kwargs = parse.call_args.kwargs
    assert kwargs["external_bitcoin_datadir"] == Path("/storage/external/bitcoin")
    assert "generate-config bitcoin " in kwargs["command"]
    assert "--disk /mnt/data --max-block 850001" in kwargs["command"]


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
