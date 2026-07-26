import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))

from client.artifacts import (  # noqa: E402
    ArtifactTransportError,
    REQUIRED_EXPORTERS,
    STAGED_EXPORTERS_COMPLETE,
    STAGED_EXPORTERS_MISSING,
    STAGED_EXPORTERS_PARTIAL,
)
from client.kubernetes import (  # noqa: E402
    S3_JOB_OWNED_RESOURCE_TYPES,
    apply_s3_emulation_resources,
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
    ensure_staged_exporters,
    pbs_stages_need_exporters,
    run_kubernetes_s3_emulation,
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


@pytest.fixture(autouse=True)
def stub_exporter_staging():
    """`run_pbs_from_s3` checks the run prefix for exporters before submitting.

    The submission tests are about job wiring and must not reach a real bucket;
    the staging decision itself is covered directly further down.
    """
    with (
        mock.patch("client.wrapper.ensure_staged_exporters"),
        mock.patch("client.wrapper.clear_s3_stage_markers"),
    ):
        yield


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


def test_s3_joinmarket_controller_enables_descriptor_regtest_fallback() -> None:
    joinmarket_manifest = render_kubernetes_manifest(engine="joinmarket")
    joinmarket_job = next(
        item for item in joinmarket_manifest["items"] if item["kind"] == "Job"
    )
    joinmarket_controller = next(
        container
        for container in joinmarket_job["spec"]["template"]["spec"]["containers"]
        if container["name"] == "controller"
    )
    assert "--joinmarket-descriptor-regtest-fallback" in joinmarket_controller["command"][-1]

    wasabi_manifest = render_kubernetes_manifest(engine="wasabi")
    wasabi_job = next(item for item in wasabi_manifest["items"] if item["kind"] == "Job")
    wasabi_controller = next(
        container
        for container in wasabi_job["spec"]["template"]["spec"]["containers"]
        if container["name"] == "controller"
    )
    assert "--joinmarket-descriptor-regtest-fallback" not in wasabi_controller["command"][-1]


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
        # The marker upload runs in the EXIT trap; a transient s5cmd failure
        # there must not abort the trap before the marker is written locally.
        assert "trap - EXIT TERM\n  set +e" in script
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    # Stale markers are cleared strictly on the frontend before qsub, never by
    # a PBS job that could race a newer submission.
    assert ".pbs/coinjoin-analysis.done" in coinjoin
    assert ".pbs/coinjoin-analysis.failed" in coinjoin
    assert " rm " not in coinjoin
    assert ".pbs/blocksci.done" in blocksci
    assert ".pbs/blocksci.failed" in blocksci
    assert ".pbs/unified-report.done" in report
    assert ".pbs/unified-report.failed" in report
    assert " rm " not in report
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
    assert "--cleanenv" in blocksci
    assert "--env PYTHONPATH=" not in blocksci
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
    assert "blocksci_export/analysis.py" in analyze
    assert "--cleanenv" in analyze
    assert "--env PYTHONPATH=" not in analyze
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
    script = next(
        container for container in init_containers if container["name"] == "prefix-preflight"
    )["command"][-1]
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
    result = run_prefix_preflight(
        STAGED_KEYS_LISTING + "2026/07/26 04:00:00  512  .k8s/upload.done\n"
    )
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
    return SimpleNamespace(
        artifact_uri="s3://bucket/runs",
        run_id="run-9",
        s3_endpoint_url="https://s3.cl4.du.cesnet.cz",
        s3_credentials_file="/storage/user/.aws/credentials",
        s3_profile="coinjoin",
    )


def test_standalone_s3_emulation_can_supply_frontend_credentials() -> None:
    # stage_kubernetes_s3_run reads args.s3_credentials_file/s3_profile before
    # creating the Job. The emulate parser used to define neither, so a live
    # (non-dry) standalone S3 emulation died with AttributeError.
    arguments = [
        "emulate", "--engine", "wasabi", "--driver", "kubernetes",
        "--artifact-backend", "s3", "--artifact-uri", "s3://bucket/runs",
        "--s3-endpoint-url", "https://s3.example.invalid",
        "--s3-secret-name", "coinjoin-s3", "--run-id", "run-1", "--reuse-namespace",
        "--kubeconfig", "/dev/null",
    ]
    parser = build_parser()
    args = parser.parse_args(
        arguments + ["--s3-credentials-file", "/storage/user/.aws/credentials",
                     "--s3-profile", "coinjoin"]
    )
    assert args.s3_credentials_file == "/storage/user/.aws/credentials"
    assert args.s3_profile == "coinjoin"

    with pytest.raises(SystemExit):
        # And they are mandatory, not silently defaulted.
        validate_artifact_arguments(parser, parser.parse_args(arguments))


def test_s3_emulation_forwards_timeout_to_uploader_manifest(
    tmp_path: Path,
) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    args = SimpleNamespace(
        engine="wasabi",
        scenario="scenario.json",
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
            "client.wrapper.compose_env",
            return_value={"SCENARIOS_DIR": str(tmp_path)},
        ),
        mock.patch(
            "client.wrapper.container_scenario_path",
            return_value="/config/scenario.json",
        ),
        mock.patch(
            "client.wrapper.host_scenario_path",
            return_value=scenario,
        ),
        mock.patch(
            "client.wrapper.resolve_uploader_image",
            return_value="uploader:test",
        ),
        mock.patch(
            "client.wrapper.render_s3_emulation_resources",
            return_value="{}",
        ) as render,
    ):
        run_kubernetes_s3_emulation(args)

    assert render.call_args.kwargs["emulation_timeout_seconds"] == 123


def test_pbs_from_s3_stages_exporters_into_a_prefix_without_them() -> None:
    # A `--blocksci-task update` run starts from a fresh, empty prefix, but the
    # analyze and report jobs download .pipeline/exporters/ from that same one.
    with (
        mock.patch(
            "client.wrapper.staged_exporters_state",
            return_value=(STAGED_EXPORTERS_MISSING, list(REQUIRED_EXPORTERS)),
        ),
        mock.patch("client.wrapper.compose_env", return_value={"EXPORTERS_DIR": "/checkout/exporters"}),
        mock.patch("client.wrapper.upload_exporters") as upload,
    ):
        ensure_staged_exporters(exporter_staging_args())

    upload.assert_called_once()
    assert upload.call_args.args[1:3] == ("s3://bucket/runs", "run-9")


def test_pbs_from_s3_keeps_exporters_an_earlier_stage_already_ran_with() -> None:
    with (
        mock.patch(
            "client.wrapper.staged_exporters_state",
            return_value=(STAGED_EXPORTERS_COMPLETE, []),
        ),
        mock.patch("client.wrapper.upload_exporters") as upload,
    ):
        ensure_staged_exporters(exporter_staging_args())

    upload.assert_not_called()


def test_pbs_from_s3_refuses_to_mix_exporter_versions_in_one_prefix() -> None:
    # A prefix staged before the blocksci_export rename still has
    # unified_report.py, so re-staging would leave the run's stages on different
    # exporter trees.
    with (
        mock.patch(
            "client.wrapper.staged_exporters_state",
            return_value=(STAGED_EXPORTERS_PARTIAL, ["blocksci_export/analysis.py"]),
        ),
        mock.patch("client.wrapper.upload_exporters") as upload,
    ):
        with pytest.raises(ArtifactTransportError, match="fresh --run-id"):
            ensure_staged_exporters(exporter_staging_args())

    upload.assert_not_called()


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("python:3.12-slim-bookworm", "docker://python:3.12-slim-bookworm"),
        ("ghcr.io/ondrejman/x@sha256:abc", "docker://ghcr.io/ondrejman/x@sha256:abc"),
        ("docker://python:3.12", "docker://python:3.12"),
        # How the offline tests hand Apptainer a locally exported image; the
        # naive "://" test used to glue a second scheme in front of it.
        ("docker-archive:/storage/images/report.tar", "docker-archive:/storage/images/report.tar"),
        ("oras://registry.example/x:1", "oras://registry.example/x:1"),
    ],
)
def test_unified_report_image_keeps_existing_uri_schemes(reference: str, expected: str) -> None:
    from client.wrapper import resolve_unified_report_pbs_image

    args = SimpleNamespace(unified_report_image=reference)
    assert resolve_unified_report_pbs_image(args) == expected


def test_frontend_rejects_an_s5cmd_without_exclude_support() -> None:
    from client.artifacts import require_s5cmd_version

    with mock.patch("client.artifacts.s5cmd_version", return_value=(2, 0, 0)):
        with pytest.raises(ArtifactTransportError, match="too old"):
            require_s5cmd_version()
    # New enough, and an unparsable version must not block a run.
    with mock.patch("client.artifacts.s5cmd_version", return_value=(2, 3, 0)):
        require_s5cmd_version()
    with mock.patch("client.artifacts.s5cmd_version", return_value=None):
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
    from client.kubernetes import kubernetes_job_probe

    probe = kubernetes_job_probe(Path("/kube/config"), "coinjoin", "coinjoin-s3-run-1")
    for container_key, name in (
        ("initContainerStatuses", "prefix-preflight"),
        ("containerStatuses", "uploader"),
    ):
        with mock.patch(
            "client.kubernetes.subprocess.run",
            side_effect=kubectl_results({}, [waiting_pod(container_key, name, "ImagePullBackOff")]),
        ):
            assert probe() == "terminal", name


def test_job_probe_reports_queued_while_containers_are_pending() -> None:
    from client.kubernetes import kubernetes_job_probe

    probe = kubernetes_job_probe(Path("/kube/config"), "coinjoin", "coinjoin-s3-run-1")
    with mock.patch(
        "client.kubernetes.subprocess.run",
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
    assert "blocksci_export/analysis.py" in blocksci.call_args.kwargs["command"]
    assert report.call_args.kwargs["dependency_job_ids"] == (
        "analysis.server",
        "blocksci.server",
    )
    assert report.call_args.kwargs["ncpus"] == 2
    assert report.call_args.kwargs["mem"] == "8gb"
    assert report.call_args.kwargs["scratch"] == "10gb"
    assert report.call_args.kwargs["walltime"] == "01:00:00"
    # Pinned public Python image from container/unified-report.image; the
    # scheme is added by the Singularity caller, not stored in the lock file.
    assert report.call_args.kwargs["image"] == "docker://python:3.12-slim-bookworm"
    assert report.call_args.kwargs["command"] == blocksci_export_pbs_command(
        run_id="run-1",
        coinjoin_type="wasabi2",
        min_input_count=2,
        joinmarket_detector="definite",
        joinmarket_min_base_fee=5000,
        joinmarket_percentage_fee=0.00004,
        joinmarket_max_depth=200000,
        test_values=True,
        uploader_image="ghcr.io/ondrejman/coinjoin-pipeline-uploader:latest",
        unified_report_image="python:3.12-slim-bookworm",
    )
    # This job is the only channel through which the report learns the two
    # images that never touch a daemon it could inspect; without them both
    # manifest fields stay null, which is what images.wrapper used to hold.
    command = report.call_args.kwargs["command"]
    assert "--uploader-image ghcr.io/ondrejman/coinjoin-pipeline-uploader:latest" in command
    assert "--unified-report-image python:3.12-slim-bookworm" in command


def test_pbs_from_s3_clears_each_marker_immediately_before_submission() -> None:
    args = s3_pbs_args()
    events: list[str] = []

    def clear(_access, _uri, _run_id, stage):
        events.append(f"clear:{stage}")

    with (
        mock.patch("client.wrapper.clear_s3_stage_markers", side_effect=clear),
        mock.patch(
            "client.wrapper.submit_coinjoin_analysis_s3_pbs",
            side_effect=lambda **_kwargs: events.append("submit:coinjoin-analysis")
            or "analysis.server",
        ),
        mock.patch(
            "client.wrapper.submit_blocksci_s3_pbs",
            side_effect=lambda **_kwargs: events.append("submit:blocksci")
            or "blocksci.server",
        ),
        mock.patch(
            "client.wrapper.submit_unified_report_s3_pbs",
            side_effect=lambda **_kwargs: events.append("submit:unified-report")
            or "report.server",
        ),
    ):
        run_pbs_from_s3(args)

    assert events == [
        "clear:coinjoin-analysis",
        "submit:coinjoin-analysis",
        "clear:blocksci",
        "submit:blocksci",
        "clear:unified-report",
        "submit:unified-report",
    ]


def test_pbs_from_s3_rolls_back_jobs_when_later_preparation_fails() -> None:
    args = s3_pbs_args()

    def clear(_access, _uri, _run_id, stage):
        if stage == "unified-report":
            raise ArtifactTransportError("S3 unavailable")

    with (
        mock.patch("client.wrapper.clear_s3_stage_markers", side_effect=clear),
        mock.patch(
            "client.wrapper.submit_coinjoin_analysis_s3_pbs",
            return_value="analysis.server",
        ),
        mock.patch(
            "client.wrapper.submit_blocksci_s3_pbs",
            return_value="blocksci.server",
        ),
        mock.patch("client.wrapper.submit_unified_report_s3_pbs") as report,
        mock.patch("client.wrapper.qdel_pbs_job") as qdel,
        pytest.raises(ArtifactTransportError, match="S3 unavailable"),
    ):
        run_pbs_from_s3(args)

    report.assert_not_called()
    assert qdel.call_args_list == [
        mock.call("blocksci.server"),
        mock.call("analysis.server"),
    ]


def test_pbs_from_s3_persists_each_job_id_for_overlap_detection(
    tmp_path: Path,
) -> None:
    args = s3_pbs_args(analysis=True, blocksci=False)
    args.pbs_submission_dir = tmp_path

    with mock.patch(
        "client.wrapper.submit_coinjoin_analysis_s3_pbs",
        return_value="analysis.server",
    ):
        run_pbs_from_s3(args)

    assert (tmp_path / ".pbs" / "coinjoin-analysis.jobid").read_text(
        encoding="utf-8"
    ) == "analysis.server\n"


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
        mock.patch("client.wrapper.s3_access_preflight"),
        mock.patch(
            "client.wrapper.s3_object_exists", return_value=True
        ) as baseline_probe,
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
    assert baseline_probe.call_args.args[1].endswith(
        "coinjoin-analysis_data/coinjoin_tx_info.json"
    )


def test_pbs_from_s3_resume_without_baseline_fails_fast() -> None:
    args = s3_pbs_args(analysis=False, blocksci=False, mappings=True)
    with (
        mock.patch("client.wrapper.s3_access_preflight"),
        mock.patch("client.wrapper.s3_object_exists", return_value=False),
        mock.patch("client.wrapper.submit_mappings_s3_pbs") as mappings,
    ):
        with pytest.raises(ArtifactTransportError, match="requires an existing"):
            run_pbs_from_s3(args)
    mappings.assert_not_called()


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


def test_pbs_from_s3_stage_resources_override_shared_fallback_independently() -> None:
    args = s3_pbs_args(mappings=True)
    args.pbs_ncpus = 4
    args.pbs_mem = "24gb"
    args.pbs_scratch = "50gb"
    args.pbs_walltime = "04:00:00"
    args.pbs_analysis_ncpus = 8
    args.pbs_analysis_mem = "32gb"
    args.pbs_analysis_scratch = "100gb"
    args.pbs_analysis_walltime = "08:00:00"
    args.pbs_blocksci_ncpus = 32
    args.pbs_blocksci_mem = "2tb"
    args.pbs_blocksci_scratch = "2tb"
    args.pbs_blocksci_walltime = "48:00:00"
    args.pbs_mappings_ncpus = 6
    args.pbs_mappings_mem = "16gb"
    args.pbs_mappings_scratch = "40gb"
    args.pbs_mappings_walltime = "02:00:00"
    with (
        mock.patch(
            "client.wrapper.submit_coinjoin_analysis_s3_pbs",
            return_value="analysis.server",
        ) as analysis,
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
        ),
    ):
        run_pbs_from_s3(args)

    assert analysis.call_args.kwargs["ncpus"] == 8
    assert analysis.call_args.kwargs["mem"] == "32gb"
    assert analysis.call_args.kwargs["scratch"] == "100gb"
    assert analysis.call_args.kwargs["walltime"] == "08:00:00"
    assert blocksci.call_args.kwargs["ncpus"] == 32
    assert blocksci.call_args.kwargs["mem"] == "2tb"
    assert blocksci.call_args.kwargs["scratch"] == "2tb"
    assert blocksci.call_args.kwargs["walltime"] == "48:00:00"
    assert mappings.call_args.kwargs["ncpus"] == 6
    assert mappings.call_args.kwargs["mem"] == "16gb"
    assert mappings.call_args.kwargs["scratch"] == "40gb"
    assert mappings.call_args.kwargs["walltime"] == "02:00:00"


def test_pbs_from_s3_blocksci_only_keeps_combined_report() -> None:
    args = s3_pbs_args(analysis=False)
    with (
        mock.patch("client.wrapper.s3_access_preflight"),
        mock.patch("client.wrapper.s3_object_exists", return_value=True),
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
    uploader_env = {
        item["name"]: item for item in containers["uploader"]["env"]
    }
    assert uploader_env["EMULATION_TIMEOUT_SECONDS"]["value"] == "21600"
    assert "JOB_NAME" not in uploader_env
    assert (
        "controller exceeded emulation timeout"
        in containers["uploader"]["command"][-1]
    )
    assert 'delete job "$JOB_NAME"' not in containers["uploader"]["command"][-1]
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


def test_apply_s3_resources_attaches_job_ownership_to_support_objects(
    tmp_path: Path,
) -> None:
    manifest = json.dumps(render_kubernetes_manifest())
    completed = subprocess.CompletedProcess([], 0, "", "")
    uid_result = subprocess.CompletedProcess([], 0, "job-uid-1", "")

    with mock.patch(
        "client.kubernetes.subprocess.run",
        side_effect=[
            completed,
            uid_result,
            *[completed for _ in S3_JOB_OWNED_RESOURCE_TYPES],
        ],
    ) as run:
        apply_s3_emulation_resources(manifest, tmp_path / "kubeconfig")

    patch_calls = run.call_args_list[2:]
    assert len(patch_calls) == len(S3_JOB_OWNED_RESOURCE_TYPES)
    assert [call.args[0][6] for call in patch_calls] == list(
        S3_JOB_OWNED_RESOURCE_TYPES
    )
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
            "client.kubernetes.subprocess.run",
            side_effect=[completed, uid_result, patch_failure],
        ),
        mock.patch("client.kubernetes.delete_s3_emulation_job") as delete_job,
        mock.patch(
            "client.kubernetes.delete_s3_emulation_support_resources"
        ) as delete_support,
        pytest.raises(RuntimeError, match="could not attach Job ownership"),
    ):
        apply_s3_emulation_resources(manifest, tmp_path / "kubeconfig")

    resource_name = s3_emulation_job_name("run-1")
    delete_job.assert_called_once_with(
        tmp_path / "kubeconfig", "coinjoin", resource_name
    )
    delete_support.assert_called_once_with(
        tmp_path / "kubeconfig", "coinjoin", resource_name
    )


def test_kubernetes_manifest_reuses_existing_namespace() -> None:
    manifest = render_kubernetes_manifest(reuse_namespace=True)

    assert all(item["kind"] != "Namespace" for item in manifest["items"])
    assert all(
        item["metadata"].get("namespace") == "coinjoin" for item in manifest["items"]
    )


def exporter_need_args(**overrides) -> SimpleNamespace:
    defaults = dict(
        analysisPbs=False,
        blocksciPbs=True,
        mappingsPbs=False,
        blocksci_workflow="reusable",
        blocksci_task="detect",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_only_stages_that_run_the_exporters_require_them() -> None:
    # Baseline, mappings, parse, update, script and notebook jobs bind an empty
    # exporters directory at most; staging for them would let a local exporter
    # problem block a job that cannot use one.
    assert pbs_stages_need_exporters(exporter_need_args()) is True
    assert pbs_stages_need_exporters(exporter_need_args(blocksci_workflow="combined")) is True
    assert (
        pbs_stages_need_exporters(
            exporter_need_args(blocksci_workflow="combined", blocksci_task="notebook")
        )
        is True
    )
    for overrides in (
        dict(blocksciPbs=False, analysisPbs=True),
        dict(blocksciPbs=False, mappingsPbs=True),
        dict(blocksci_task="parse"),
        dict(blocksci_task="script"),
        dict(blocksci_task="notebook"),
        dict(blocksci_workflow="cached", blocksci_task="update"),
    ):
        assert pbs_stages_need_exporters(exporter_need_args(**overrides)) is False, overrides


def test_pbs_from_s3_skips_staging_for_stages_that_never_run_exporters() -> None:
    args = s3_pbs_args(analysis=True, blocksci=False)
    args.stage_exporters = False
    with (
        mock.patch("client.wrapper.ensure_staged_exporters") as staging,
        mock.patch("client.wrapper.submit_coinjoin_analysis_s3_pbs", return_value="1.job"),
    ):
        run_pbs_from_s3(args)

    staging.assert_not_called()


def test_stage_exporters_flag_pre_stages_for_a_later_detect_run() -> None:
    args = s3_pbs_args(analysis=True, blocksci=False)
    args.stage_exporters = True
    with (
        mock.patch("client.wrapper.ensure_staged_exporters") as staging,
        mock.patch("client.wrapper.submit_coinjoin_analysis_s3_pbs", return_value="1.job"),
    ):
        run_pbs_from_s3(args)

    staging.assert_called_once()
