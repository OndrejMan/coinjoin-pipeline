import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest
from config_support import configuration as Namespace

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from config_support import build_parser

from coinjoin_pipeline.execution.run_context import run_dir_under_root
from coinjoin_pipeline.execution.scenarios import container_scenario_path, host_scenario_path
from coinjoin_pipeline.execution.containers import (
    compose_env,
    container_run_pull_args,
    exists_or_unreadable,
    export_command,
    export_preflight_error,
    run_blocksci_docker_stage,
    run_coinjoin_analysis,
    run_command,
    run_dirs,
    stage_blocksci_script,
    stage_pbs_exporters,
)
from coinjoin_pipeline.execution.kubernetes import kubernetes_auth_preflight
from coinjoin_pipeline.execution.kubernetes_launch import (
    kubernetes_emulator_command,
    run_kubernetes_emulation,
)
from coinjoin_pipeline.execution.locks import command_lock_path, ensure_no_active_s3_pbs_submission
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pipeline_logging import (
    captured_pipeline_stage,
    pipeline_stage,
    stage_separator,
    terminal_supports_color,
)
from coinjoin_pipeline.execution.runtime import (
    compose_command,
    container_runtime,
)
from coinjoin_pipeline.execution.s3_submission import S3PBSJobs
from coinjoin_pipeline.execution.s3_workflow import run_s3_full_run as run_full_run_s3
from coinjoin_pipeline.execution.settings import (
    COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV,
    COINJOIN_ANALYSIS_MOUNT_PATH_ENV,
    COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER,
    COINJOIN_ANALYSIS_SOURCE_PATH_ENV,
    COINJOIN_ANALYSIS_TARGET_PATH_ENV,
    RUNS_ROOT_CONTAINER,
)
from coinjoin_pipeline.execution.shared_storage_pbs import (
    run_blocksci_stage as run_blocksci_pbs_stage,
)
from coinjoin_pipeline.storage.s3 import ArtifactTransportError


def _write_required_exporters(root: Path) -> None:
    from coinjoin_pipeline.storage.s3 import REQUIRED_EXPORTERS

    for relative in REQUIRED_EXPORTERS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n")
    (root / "blocksci_export").mkdir(parents=True, exist_ok=True)
    (root / "unified_report.py").write_text("report\n", encoding="utf-8")
    (root / "blocksci_export" / "analysis.py").write_text("analysis\n", encoding="utf-8")


def test_stage_pbs_exporters_snapshots_checkout_under_shared_run(
    tmp_path: Path,
) -> None:
    source = tmp_path / "checkout-exporters"
    run_dir = tmp_path / "run-a"
    _write_required_exporters(source)
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "module.pyc").write_bytes(b"bytecode")

    staged = stage_pbs_exporters(run_dir, source)

    assert staged == run_dir / ".pipeline" / "exporters"
    assert (staged / "unified_report.py").read_text(encoding="utf-8") == "report\n"
    assert (staged / "blocksci_export" / "analysis.py").is_file()
    assert not (staged / "__pycache__").exists()

    (source / "newer.py").write_text("newer\n", encoding="utf-8")
    assert stage_pbs_exporters(run_dir, source) == staged
    assert not (staged / "newer.py").exists()


def test_stage_pbs_exporters_rejects_partial_existing_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "checkout-exporters"
    run_dir = tmp_path / "run-a"
    _write_required_exporters(source)
    partial = run_dir / ".pipeline" / "exporters"
    partial.mkdir(parents=True)
    (partial / "unified_report.py").write_text("report\n", encoding="utf-8")

    with pytest.raises(PBSError, match="Failed to stage PBS exporters"):
        stage_pbs_exporters(run_dir, source)


def test_blocksci_pbs_stage_submits_shared_staged_exporters(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "run-a"
    run_dir.mkdir(parents=True)
    bitcoin_datadir = tmp_path / "bitcoin"
    args = build_parser().parse_args(
        [
            "analyze",
            "--engine",
            "wasabi",
            "--run-dir",
            "run-a",
            "--blocksciPbs",
            "--pbs-bitcoin-datadir",
            str(bitcoin_datadir),
        ]
    )
    staged = run_dir / ".pipeline" / "exporters"

    with (
        mock.patch(
            "coinjoin_pipeline.execution.containers.compose_env",
            return_value={
                "EMULATION_LOGS_DIR": str(run_dir.parent),
                "EXPORTERS_DIR": str(tmp_path / "checkout-exporters"),
            },
        ),
        mock.patch(
            "coinjoin_pipeline.execution.containers.stage_pbs_exporters",
            return_value=staged,
        ) as stage_mock,
        mock.patch("coinjoin_pipeline.execution.shared_storage_pbs.submit_blocksci_pbs") as submit_mock,
        mock.patch("coinjoin_pipeline.execution.shared_storage_pbs.wait_for_pbs_marker"),
    ):
        run_blocksci_pbs_stage(args, run_dir)

    stage_mock.assert_called_once_with(
        run_dir,
        (tmp_path / "checkout-exporters").resolve(),
    )
    assert submit_mock.call_args.kwargs["exporters_dir"] == staged


def _kubectl_cmd(*parts: str) -> list[str]:
    return ["kubectl", "--kubeconfig", "/kube/config", *parts]


def test_pbs_from_s3_uses_a_run_specific_submission_lock(tmp_path: Path) -> None:
    args = Namespace(action="pbs-from-s3", run_id="run-1")

    assert command_lock_path(args, tmp_path) == (tmp_path / "run-1" / ".pbs-submit.lock")


def test_pbs_from_s3_refuses_a_recorded_active_graph(tmp_path: Path) -> None:
    marker_dir = tmp_path / ".pbs"
    marker_dir.mkdir()
    (marker_dir / "blocksci.jobid").write_text("blocksci.server\n", encoding="utf-8")

    with (
        mock.patch(
            "coinjoin_pipeline.execution.locks.pbs_job_probe",
            return_value=lambda: "running",
        ),
        pytest.raises(RuntimeError, match="still active"),
    ):
        ensure_no_active_s3_pbs_submission(tmp_path)


def _full_run_s3_args(**overrides) -> Namespace:
    values = dict(
        artifact_uri="s3://bucket/runs",
        run_id="run-1",
        s3_endpoint_url="https://s3.cl4.du.cesnet.cz",
        s3_credentials_file="/storage/user/.aws/credentials",
        s3_profile="coinjoin",
        s3_secret_name="coinjoin-s3",
        kubeconfig=None,
        namespace="coinjoin-ns",
        reuse_namespace=True,
        dry_run=False,
        emulation_timeout=3600,
        analysisPbs=True,
        blocksciPbs=True,
        mappingsPbs=False,
        pbs_walltime=None,
    )
    values.update(overrides)
    return Namespace(**values)


class FullRunS3OrchestrationTest(unittest.TestCase):
    def _patches(self):
        workflow = "coinjoin_pipeline.execution.s3_workflow"
        markers = "coinjoin_pipeline.execution.s3_markers"
        wait_for_marker = mock.MagicMock()
        targets = {
            "require_qsub": f"{workflow}.require_qsub",
            "stage_kubernetes_s3_run": f"{workflow}.stage_kubernetes_s3_run",
            "run_kubernetes_s3_emulation": f"{workflow}.run_s3_kubernetes_emulation",
            "run_pbs_from_s3": f"{workflow}.submit_s3_pbs_graph",
            "kubernetes_job_probe": f"{workflow}.kubernetes_job_probe",
            "collect_s3_emulation_diagnostics": f"{workflow}.collect_s3_emulation_diagnostics",
            "delete_s3_emulation_job": f"{workflow}.delete_s3_emulation_job",
            "pbs_job_probe": f"{markers}.pbs_job_probe",
            "qdel_pbs_job": f"{markers}.qdel_pbs_job",
        }
        patches = {name: mock.patch(target) for name, target in targets.items()}
        # Emulation and PBS stage waits share one marker transport.
        patches["wait_for_s3_marker"] = mock.patch(f"{workflow}.wait_for_s3_marker", wait_for_marker)
        patches["wait_for_s3_marker_in_markers"] = mock.patch(f"{markers}.wait_for_s3_marker", wait_for_marker)
        return patches

    def test_full_run_s3_waits_between_stages_in_order(self):
        patches = self._patches()
        mocks = {name: patcher.start() for name, patcher in patches.items()}
        self.addCleanup(mock.patch.stopall)
        mocks["run_pbs_from_s3"].return_value = S3PBSJobs(
            coinjoin_analysis="analysis.job",
            blocksci_work="blocksci.job",
            unified_report="report.job",
        )
        calls: list[str] = []
        mocks["run_kubernetes_s3_emulation"].side_effect = lambda *a, **k: calls.append("emulate")
        mocks["run_pbs_from_s3"].side_effect = lambda *a, **k: (
            calls.append("submit-pbs"),
            S3PBSJobs(
                coinjoin_analysis="analysis.job",
                blocksci_work="blocksci.job",
                unified_report="report.job",
            ),
        )[1]
        mocks["wait_for_s3_marker"].side_effect = lambda stage, *a, **k: calls.append(f"wait:{stage}")

        run_full_run_s3(_full_run_s3_args())

        self.assertEqual(
            calls,
            [
                "emulate",
                "wait:kubernetes-emulation",
                "submit-pbs",
                "wait:coinjoin-analysis",
                "wait:blocksci",
                "wait:unified-report",
            ],
        )
        mocks["qdel_pbs_job"].assert_not_called()

    def test_full_run_s3_emulation_failure_skips_pbs_and_collects_diagnostics(self):
        patches = self._patches()
        mocks = {name: patcher.start() for name, patcher in patches.items()}
        self.addCleanup(mock.patch.stopall)
        mocks["wait_for_s3_marker"].side_effect = ArtifactTransportError("emulation failed")
        mocks["collect_s3_emulation_diagnostics"].return_value = "diagnostics"

        with self.assertRaises(ArtifactTransportError):
            run_full_run_s3(_full_run_s3_args())

        mocks["run_pbs_from_s3"].assert_not_called()
        mocks["collect_s3_emulation_diagnostics"].assert_called_once()
        mocks["delete_s3_emulation_job"].assert_called_once()

    def test_full_run_s3_waits_for_mappings_before_report(self):
        patches = self._patches()
        mocks = {name: patcher.start() for name, patcher in patches.items()}
        self.addCleanup(mock.patch.stopall)
        mocks["run_pbs_from_s3"].return_value = S3PBSJobs(
            coinjoin_analysis="analysis.job",
            coinjoin_mappings="mappings.job",
            blocksci_work="blocksci.job",
            unified_report="report.job",
        )
        calls: list[str] = []
        mocks["wait_for_s3_marker"].side_effect = lambda stage, *a, **k: calls.append(stage)

        run_full_run_s3(_full_run_s3_args(mappingsPbs=True))

        self.assertEqual(
            calls,
            [
                "kubernetes-emulation",
                "coinjoin-analysis",
                "blocksci",
                "coinjoin-mappings",
                "unified-report",
            ],
        )

    def test_full_run_s3_analysis_failure_cancels_dependent_report_job(self):
        patches = self._patches()
        mocks = {name: patcher.start() for name, patcher in patches.items()}
        self.addCleanup(mock.patch.stopall)
        mocks["run_pbs_from_s3"].return_value = S3PBSJobs(
            coinjoin_analysis="analysis.job",
            blocksci_work="blocksci.job",
            unified_report="report.job",
        )

        def wait(stage, *arguments, **keywords):
            if stage == "coinjoin-analysis":
                raise ArtifactTransportError("analysis failed")

        mocks["wait_for_s3_marker"].side_effect = wait

        with self.assertRaises(ArtifactTransportError):
            run_full_run_s3(_full_run_s3_args())

        mocks["qdel_pbs_job"].assert_called_once_with("report.job")

    def test_full_run_s3_analysis_failure_cancels_mappings_and_report(self):
        patches = self._patches()
        mocks = {name: patcher.start() for name, patcher in patches.items()}
        self.addCleanup(mock.patch.stopall)
        mocks["run_pbs_from_s3"].return_value = S3PBSJobs(
            coinjoin_analysis="analysis.job",
            coinjoin_mappings="mappings.job",
            blocksci_work="blocksci.job",
            unified_report="report.job",
        )

        def wait(stage, *arguments, **keywords):
            if stage == "coinjoin-analysis":
                raise ArtifactTransportError("analysis failed")

        mocks["wait_for_s3_marker"].side_effect = wait

        with self.assertRaises(ArtifactTransportError):
            run_full_run_s3(_full_run_s3_args(mappingsPbs=True))

        self.assertEqual(
            mocks["qdel_pbs_job"].call_args_list,
            [mock.call("mappings.job"), mock.call("report.job")],
        )

    def test_full_run_s3_blocksci_failure_cancels_dependent_report_job(self):
        patches = self._patches()
        mocks = {name: patcher.start() for name, patcher in patches.items()}
        self.addCleanup(mock.patch.stopall)
        mocks["run_pbs_from_s3"].return_value = S3PBSJobs(
            coinjoin_analysis="analysis.job",
            blocksci_work="blocksci.job",
            unified_report="report.job",
        )

        def wait(stage, *arguments, **keywords):
            if stage == "blocksci":
                raise ArtifactTransportError("blocksci failed")

        mocks["wait_for_s3_marker"].side_effect = wait

        with self.assertRaises(ArtifactTransportError):
            run_full_run_s3(_full_run_s3_args())

        mocks["qdel_pbs_job"].assert_called_once_with("report.job")

    def test_full_run_s3_parse_failure_cancels_the_whole_blocksci_branch(self):
        # A reusable workflow inserts blocksci-parse before the analyzer, so a
        # failed cache invalidates both the analyzer and the report that joins
        # it, while the independent baseline keeps running.
        patches = self._patches()
        mocks = {name: patcher.start() for name, patcher in patches.items()}
        self.addCleanup(mock.patch.stopall)
        mocks["run_pbs_from_s3"].return_value = S3PBSJobs(
            coinjoin_analysis="analysis.job",
            blocksci_parse="parse.job",
            blocksci_work="analyze.job",
            unified_report="report.job",
        )

        def wait(stage, *arguments, **keywords):
            if stage == "blocksci-parse":
                raise ArtifactTransportError("parse failed")

        mocks["wait_for_s3_marker"].side_effect = wait

        with self.assertRaises(ArtifactTransportError):
            run_full_run_s3(_full_run_s3_args(blocksci_workflow="reusable"))

        self.assertEqual(
            mocks["qdel_pbs_job"].call_args_list,
            [mock.call("analyze.job"), mock.call("report.job")],
        )

    def test_full_run_s3_mappings_failure_cancels_dependent_report_job(self):
        patches = self._patches()
        mocks = {name: patcher.start() for name, patcher in patches.items()}
        self.addCleanup(mock.patch.stopall)
        mocks["run_pbs_from_s3"].return_value = S3PBSJobs(
            coinjoin_analysis="analysis.job",
            coinjoin_mappings="mappings.job",
            blocksci_work="blocksci.job",
            unified_report="report.job",
        )

        def wait(stage, *arguments, **keywords):
            if stage == "coinjoin-mappings":
                raise ArtifactTransportError("mappings failed")

        mocks["wait_for_s3_marker"].side_effect = wait

        with self.assertRaises(ArtifactTransportError):
            run_full_run_s3(_full_run_s3_args(mappingsPbs=True))

        mocks["qdel_pbs_job"].assert_called_once_with("report.job")


class WrapperExportTest(unittest.TestCase):
    def test_stage_blocksci_script_preserves_script_in_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "analysis.py"
            source.write_text("print('custom analysis')\n", encoding="utf-8")
            run_dir = root / "run-a"
            run_dir.mkdir()

            container_path = stage_blocksci_script(str(source), run_dir)

            staged = run_dir / ".pipeline" / "blocksci-script.py"
            self.assertEqual(staged.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertEqual(
                container_path,
                "/runs/emulation/logs/run-a/.pipeline/blocksci-script.py",
            )

    def test_terminal_colors_are_disabled_for_plain_streams(self):
        stream = io.StringIO()

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(terminal_supports_color(stream))

    def test_terminal_colors_can_be_forced(self):
        stream = io.StringIO()

        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
            self.assertTrue(terminal_supports_color(stream))

    def test_stage_separator_prints_three_lines(self):
        stream = io.StringIO()

        stage_separator(stream)

        self.assertEqual(stream.getvalue().splitlines(), ["=" * 88, "=" * 88, "=" * 88])

    def test_pipeline_stage_announces_start_and_done(self):
        stream = io.StringIO()

        with mock.patch("coinjoin_pipeline.execution.pipeline_logging.sys.stdout", stream):
            with pipeline_stage("Example stage"):
                pass

        output = stream.getvalue()
        self.assertIn("[pipeline] START: Example stage", output)
        self.assertIn("[pipeline] DONE: Example stage", output)
        self.assertIn(("=" * 88) + "\n" + ("=" * 88) + "\n" + ("=" * 88), output)

    def test_captured_pipeline_stage_writes_merged_run_log_and_keeps_terminal_output(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "run-a"
            terminal = io.StringIO()

            with (
                mock.patch("coinjoin_pipeline.execution.pipeline_logging.sys.stdout", terminal),
                mock.patch("coinjoin_pipeline.execution.pipeline_logging.sys.stderr", terminal),
            ):
                with captured_pipeline_stage(root, "BlockSci analysis", run_dir) as stage_log:
                    print("standard output")
                    print("standard error", file=sys.stderr)

            log_text = stage_log.path.read_text(encoding="utf-8")
            self.assertEqual(stage_log.path.parent, run_dir / "logs")
            self.assertRegex(stage_log.path.name, r"^\d{8}T\d{6}\.\d{6}Z-blocksci-analysis\.log$")
            self.assertIn("standard output", log_text)
            self.assertIn("standard error", log_text)
            self.assertIn("standard output", terminal.getvalue())

    def test_failed_pending_stage_is_retained_under_failed_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            with self.assertRaisesRegex(RuntimeError, "emulator failed"):
                with captured_pipeline_stage(root, "Docker emulation"):
                    raise RuntimeError("emulator failed")

            failed_logs = list((root / "_failed").glob("*.log"))
            self.assertEqual(len(failed_logs), 1)
            self.assertIn(
                "[pipeline] FAILED: Docker emulation",
                failed_logs[0].read_text(encoding="utf-8"),
            )

    def test_completed_pending_emulation_log_can_be_relocated_to_its_new_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "run-created-by-emulator"

            with captured_pipeline_stage(root, "Docker emulation") as stage_log:
                print("emulator output")
            destination = stage_log.relocate_to_run(run_dir)

            self.assertEqual(destination.parent, run_dir / "logs")
            self.assertFalse((root / ".pending").exists() and any((root / ".pending").iterdir()))
            self.assertIn("emulator output", destination.read_text(encoding="utf-8"))

    def test_captured_stage_includes_child_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "run-a"
            with captured_pipeline_stage(root, "Export", run_dir) as stage_log:
                run_command(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('child stdout'); print('child stderr', file=sys.stderr)",
                    ]
                )

            log_text = stage_log.path.read_text(encoding="utf-8")
            self.assertIn("child stdout", log_text)
            self.assertIn("child stderr", log_text)

    def test_blocksci_docker_stage_is_independent_and_can_defer_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            args = Namespace(
                blocksci_script=None,
                engine="joinmarket",
                coinjoin_type="joinmarket",
                min_input_count=1,
                scenario=None,
                joinmarket_detector="definite",
                joinmarket_min_base_fee=5000,
                joinmarket_percentage_fee=0.00004,
                joinmarket_max_depth=200000,
            )
            with mock.patch("coinjoin_pipeline.execution.containers.run_command") as run_mock:
                run_blocksci_docker_stage(args, run_dir, include_report=False)

            command = run_mock.call_args.args[0]
            self.assertIn("--no-deps", command)
            self.assertEqual(command[-1], "blocksci")
            self.assertEqual(run_mock.call_args.kwargs["env"]["BLOCKSCI_EXPORT_REPORT"], "false")

    def test_container_runtime_defaults_to_docker(self):
        self.assertEqual(container_runtime({}), "docker")
        self.assertEqual(compose_command({}), ["docker", "compose"])

    def test_container_runtime_supports_podman(self):
        env = {"CONTAINER_RUNTIME": "podman"}

        self.assertEqual(container_runtime(env), "podman")
        self.assertEqual(compose_command(env), ["podman", "compose"])

    def test_compose_command_can_be_overridden(self):
        env = {
            "CONTAINER_RUNTIME": "podman",
            "CONTAINER_COMPOSE_COMMAND": "podman-compose",
        }

        self.assertEqual(compose_command(env), ["podman-compose"])

    def test_compose_env_uses_blocksci_detector_default_input_count(self):
        self.assertEqual(compose_env()["BLOCKSCI_MIN_INPUT_COUNT"], "default")

    def test_compose_env_sets_default_run_timezone(self):
        self.assertEqual(compose_env()["RUN_TIMEZONE"], "Europe/Prague")

    def test_compose_env_isolates_the_checkout_compose_project_and_notebook_port(self):
        env = compose_env()

        self.assertRegex(env["COINJOIN_COMPOSE_PROJECT"], r"^blocksci-emulator-[0-9a-f]{12}$")
        self.assertGreaterEqual(int(env["BLOCKSCI_HOST_PORT"]), 20000)
        self.assertLessEqual(int(env["BLOCKSCI_HOST_PORT"]), 29999)

    def test_compose_env_allows_run_timezone_override(self):
        self.assertEqual(compose_env(Namespace(run_timezone="UTC"))["RUN_TIMEZONE"], "UTC")

    def test_compose_env_targets_active_run_for_coinjoin_analysis(self):
        env = compose_env(active_run_id="run-a")

        self.assertEqual(
            env[COINJOIN_ANALYSIS_SOURCE_PATH_ENV],
            str(Path(env["EMULATION_LOGS_DIR"]) / "run-a" / "coinjoin-analysis_data"),
        )
        self.assertEqual(
            env[COINJOIN_ANALYSIS_MOUNT_PATH_ENV],
            f"{COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER}/run-a",
        )
        self.assertEqual(
            env[COINJOIN_ANALYSIS_TARGET_PATH_ENV],
            COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER,
        )
        self.assertEqual(
            env[COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV],
            str(Path(env["EMULATION_LOGS_DIR"]) / "run-a" / "coinjoin_emulator_data" / "data"),
        )

    def test_compose_env_targets_active_run_with_emulation_logs_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "coinjoin-pipeline" / "coinjoin-runs"
            with mock.patch.dict(os.environ, {"EMULATION_LOGS_DIR": str(logs_dir)}):
                env = compose_env(active_run_id="run-a")

            self.assertEqual(env["EMULATION_LOGS_DIR"], str(logs_dir.resolve()))
            self.assertEqual(
                env[COINJOIN_ANALYSIS_SOURCE_PATH_ENV],
                str(logs_dir.resolve() / "run-a" / "coinjoin-analysis_data"),
            )

    def test_compose_env_without_active_run_has_no_analysis_mounts(self):
        env = compose_env()

        self.assertNotIn(COINJOIN_ANALYSIS_SOURCE_PATH_ENV, env)
        self.assertNotIn(COINJOIN_ANALYSIS_MOUNT_PATH_ENV, env)
        self.assertNotIn(COINJOIN_ANALYSIS_TARGET_PATH_ENV, env)
        self.assertNotIn(COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV, env)

    def test_export_command_uses_explicit_minimum_input_count(self):
        env = {
            "SCENARIO_FALLBACK_PATH": "/mnt/scenarios/defaultCoinJoin.json",
            "BLOCKSCI_COINJOIN_TYPE": "wasabi2",
            "BLOCKSCI_MIN_INPUT_COUNT": "1",
            "BLOCKSCI_JOINMARKET_DETECTOR": "definite",
            "BLOCKSCI_JOINMARKET_MIN_BASE_FEE": "5000",
            "BLOCKSCI_JOINMARKET_PERCENTAGE_FEE": "0.00004",
            "BLOCKSCI_JOINMARKET_MAX_DEPTH": "200000",
        }

        command = export_command("run-a", env)

        self.assertIn("--min-input-count 1", command)
        self.assertIn("/mnt/exporters/worker.py report", command)
        self.assertIn("--joinmarket-detector", command)
        self.assertIn(f"--run-dir {RUNS_ROOT_CONTAINER}/run-a", command)
        self.assertIn(f"{RUNS_ROOT_CONTAINER}/run-a", command)

    def test_export_command_does_not_accept_test_value_environment(self):
        env = {
            "SCENARIO_FALLBACK_PATH": "/mnt/scenarios/defaultCoinJoin.json",
            "BLOCKSCI_COINJOIN_TYPE": "wasabi2",
            "BLOCKSCI_MIN_INPUT_COUNT": "1",
            "BLOCKSCI_TEST_VALUES": "true",
            "BLOCKSCI_JOINMARKET_DETECTOR": "definite",
            "BLOCKSCI_JOINMARKET_MIN_BASE_FEE": "5000",
            "BLOCKSCI_JOINMARKET_PERCENTAGE_FEE": "0.00004",
            "BLOCKSCI_JOINMARKET_MAX_DEPTH": "200000",
        }

        self.assertNotIn("--test-values", export_command("run-a", env))

    @unittest.skipIf(os.geteuid() == 0, "root can read every directory")
    def test_blocksci_output_survives_a_root_only_parsed_directory(self):
        # blocksci_parser creates parsed/ with mode 0700 while the container runs
        # as root. The bare wrapper checks it as the invoking user, and is_file()
        # reports EACCES as False — a finished run then looks like it never ran.
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "2026-07-26_07-25_overactive-local"
            (run_dir / "blocksci_data").mkdir(parents=True)
            (run_dir / "blocksci_data" / "config.json").write_text("{}", encoding="utf-8")
            parsed = run_dir / "blocksci_data" / "parsed" / "chain"
            parsed.mkdir(parents=True)
            (parsed / "block.dat").write_bytes(b"chain")
            parsed.parent.chmod(0o000)
            try:
                self.assertTrue(exists_or_unreadable(parsed / "block.dat"))
            finally:
                parsed.parent.chmod(0o700)

    def test_blocksci_output_still_missing_when_the_stage_never_ran(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-1"
            (run_dir / "blocksci_data").mkdir(parents=True)
            (run_dir / "blocksci_data" / "config.json").write_text("{}", encoding="utf-8")

            self.assertFalse(exists_or_unreadable(run_dir / "blocksci_data" / "parsed" / "chain" / "block.dat"))

    def test_export_preflight_all_ready(self):
        error = export_preflight_error(
            coinjoin_ready=True,
            blocksci_ready=True,
            run_dir=Path("/tmp/run"),
        )

        self.assertIsNone(error)

    def test_export_preflight_coinjoin_only(self):
        error = export_preflight_error(
            coinjoin_ready=True,
            blocksci_ready=False,
            run_dir=Path("/tmp/2026-05-24_16-58_default"),
        )

        assert error is not None
        self.assertIn("blocksci-analysis_data/blocksci_analysis.json", error)
        self.assertNotIn("coinjoin_tx_info.json", error)
        self.assertIn("cjp analyze --run-dir 2026-05-24_16-58_default", error)

    def test_export_preflight_blocksci_only(self):
        error = export_preflight_error(
            coinjoin_ready=False,
            blocksci_ready=True,
            run_dir=Path("/tmp/2026-05-24_16-58_default"),
        )

        assert error is not None
        self.assertIn("coinjoin-analysis_data/coinjoin_tx_info.json", error)
        self.assertNotIn("blocksci_analysis.json", error)
        self.assertIn(
            "cjp coinjoin-analysis --run-dir 2026-05-24_16-58_default",
            error,
        )

    def test_export_preflight_neither_ready(self):
        error = export_preflight_error(
            coinjoin_ready=False,
            blocksci_ready=False,
            run_dir=Path("/tmp/2026-05-24_16-58_default"),
        )

        assert error is not None
        self.assertIn("missing analytical input", error)
        self.assertIn("coinjoin-analysis_data/coinjoin_tx_info.json", error)
        self.assertIn("blocksci-analysis_data/blocksci_analysis.json", error)

    def test_run_dirs_only_includes_grouped_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scenario_run = root / "scenario-run"
            coinjoin_run = root / "coinjoin-run"
            empty_dir = root / "empty-dir"
            scenario_run.mkdir()
            coinjoin_run.mkdir()
            empty_dir.mkdir()
            (scenario_run / "scenario.json").write_text("{}", encoding="utf-8")
            (coinjoin_run / "coinjoin_tx_info.json").write_text("{}", encoding="utf-8")

            found = {path.name for path in run_dirs(root)}

        self.assertEqual(found, set())

    def test_run_dirs_includes_grouped_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            grouped_run = root / "grouped-run"
            maintenance_dir = root / "_maintenance"
            failed_dir = root / "_failed"
            (grouped_run / "coinjoin_emulator_data").mkdir(parents=True)
            (grouped_run / "coinjoin_emulator_data" / "scenario.json").write_text("{}", encoding="utf-8")
            maintenance_dir.mkdir()
            failed_dir.mkdir()

            found = {path.name for path in run_dirs(root)}

        self.assertEqual(found, {"grouped-run"})

    def test_detect_active_run_requires_pinned_run_dir(self):
        from coinjoin_pipeline.execution.containers import detect_active_run

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stale_run = root / "2026-07-09_09-07_default-joinmarket"
            (stale_run / "coinjoin_emulator_data").mkdir(parents=True)
            (stale_run / "coinjoin_emulator_data" / "scenario.json").write_text("{}", encoding="utf-8")
            pinned = root / "2026-07-12_22-37_default-joinmarket"
            pinned.mkdir()
            (pinned / "research_manifest.json").write_text("{}", encoding="utf-8")

            with mock.patch.dict(os.environ, {"PIPELINE_RUN_ID": pinned.name}):
                # The emulator never stored artifacts: the stale run must not win.
                self.assertIsNone(detect_active_run(root, set()))

                (pinned / "coinjoin_emulator_data").mkdir()
                (pinned / "coinjoin_emulator_data" / "scenario.json").write_text("{}", encoding="utf-8")
                self.assertEqual(detect_active_run(root, set()), pinned.resolve())

    def test_detect_active_run_falls_back_without_pinned_id(self):
        from coinjoin_pipeline.execution.containers import detect_active_run

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run = root / "2026-07-12_22-37_default-joinmarket"
            (run / "coinjoin_emulator_data").mkdir(parents=True)
            (run / "coinjoin_emulator_data" / "scenario.json").write_text("{}", encoding="utf-8")

            environment = {key: value for key, value in os.environ.items() if key != "PIPELINE_RUN_ID"}
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(detect_active_run(root, set()), run.resolve())

    def test_run_coinjoin_analysis_targets_requested_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            checkout_dir = root / "checkout"
            run_dir = root / "emulation_logs" / "run-a"
            checkout_dir.mkdir()
            run_dir.mkdir(parents=True)
            (run_dir / "coinjoin_emulator_data" / "data").mkdir(parents=True)
            (run_dir / "coinjoin_emulator_data" / "scenario.json").write_text("{}", encoding="utf-8")

            env = {
                "SCENARIOS_DIR": str(checkout_dir / "scenarios"),
                "EMULATION_LOGS_DIR": str(root / "emulation_logs"),
                "CONTAINER_RUNTIME": "docker",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("coinjoin_pipeline.execution.containers.run_command") as run_mock,
            ):
                run_coinjoin_analysis("run-a")

            run_env = run_mock.call_args.kwargs["env"]
            self.assertEqual(
                run_env[COINJOIN_ANALYSIS_SOURCE_PATH_ENV],
                str(run_dir / "coinjoin-analysis_data"),
            )
            self.assertEqual(
                run_env[COINJOIN_ANALYSIS_MOUNT_PATH_ENV],
                f"{COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER}/run-a",
            )
            self.assertEqual(
                run_env[COINJOIN_ANALYSIS_TARGET_PATH_ENV],
                COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER,
            )
            self.assertEqual(
                run_env[COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV],
                str(run_dir / "coinjoin_emulator_data" / "data"),
            )
            self.assertIn("coinjoin_analysis", run_mock.call_args.args[0])

    def test_run_coinjoin_analysis_all_runs_processes_each_grouped_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            checkout_dir = root / "checkout"
            checkout_dir.mkdir()
            for run_id in ("run-a", "run-b"):
                run_dir = root / "emulation_logs" / run_id / "coinjoin_emulator_data"
                (run_dir / "data").mkdir(parents=True)
                (run_dir / "scenario.json").write_text("{}", encoding="utf-8")

            env = {
                "SCENARIOS_DIR": str(checkout_dir / "scenarios"),
                "EMULATION_LOGS_DIR": str(root / "emulation_logs"),
                "CONTAINER_RUNTIME": "docker",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("coinjoin_pipeline.execution.containers.run_command") as run_mock,
            ):
                run_coinjoin_analysis(all_runs=True)

            compose_calls = [call for call in run_mock.call_args_list if "coinjoin_analysis" in call.args[0]]
            self.assertEqual(len(compose_calls), 2)
            analysis_sources = {call.kwargs["env"][COINJOIN_ANALYSIS_SOURCE_PATH_ENV] for call in compose_calls}
            self.assertEqual(
                analysis_sources,
                {
                    str(root / "emulation_logs" / "run-a" / "coinjoin-analysis_data"),
                    str(root / "emulation_logs" / "run-b" / "coinjoin-analysis_data"),
                },
            )

    def test_run_coinjoin_analysis_analyze_only_sets_compose_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            checkout_dir = root / "checkout"
            run_dir = root / "emulation_logs" / "run-a"
            checkout_dir.mkdir()
            (run_dir / "coinjoin_emulator_data" / "data").mkdir(parents=True)
            analysis_dir = run_dir / "coinjoin-analysis_data"
            analysis_dir.mkdir()
            (analysis_dir / "coinjoin_tx_info.json").write_text("{}", encoding="utf-8")

            env = {
                "SCENARIOS_DIR": str(checkout_dir / "scenarios"),
                "EMULATION_LOGS_DIR": str(root / "emulation_logs"),
                "CONTAINER_RUNTIME": "docker",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("coinjoin_pipeline.execution.containers.run_command") as run_mock,
            ):
                run_coinjoin_analysis("run-a", analysis_action="analyze_only")

            self.assertEqual(
                run_mock.call_args.kwargs["env"]["COINJOIN_ANALYSIS_ACTION"],
                "analyze_only",
            )

    def test_run_coinjoin_analysis_analyze_only_requires_existing_baseline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            checkout_dir = root / "checkout"
            run_dir = root / "emulation_logs" / "run-a"
            checkout_dir.mkdir()
            (run_dir / "coinjoin_emulator_data" / "data").mkdir(parents=True)
            (run_dir / "coinjoin_emulator_data" / "scenario.json").write_text("{}", encoding="utf-8")

            env = {
                "SCENARIOS_DIR": str(checkout_dir / "scenarios"),
                "EMULATION_LOGS_DIR": str(root / "emulation_logs"),
                "CONTAINER_RUNTIME": "docker",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("coinjoin_pipeline.execution.containers.run_command") as run_mock,
                self.assertRaises(SystemExit) as raised,
            ):
                run_coinjoin_analysis("run-a", analysis_action="analyze_only")

            self.assertEqual(raised.exception.code, 2)
            run_mock.assert_not_called()

    def test_kubernetes_emulator_command_places_driver_before_subcommand(self):
        command = kubernetes_emulator_command(
            Namespace(namespace="coinjoin-test", image_prefix="ghcr.io/test/"),
            "/app/scenarios/overactive-local.json",
            btc_data_path="/btc-data/custom",
        )

        self.assertEqual(
            command,
            [
                "python",
                "manager.py",
                "--engine",
                "wasabi",
                "--driver",
                "kubernetes",
                "--run-timezone",
                "Europe/Prague",
                "run",
                "--scenario",
                "/app/scenarios/overactive-local.json",
                "--namespace",
                "coinjoin-test",
                "--image-prefix",
                "ghcr.io/test/",
                "--control-ip",
                "host.docker.internal",
                "--btc-node-arg=-blocksxor=0",
                "--btcFolder",
                "/btc-data/custom",
            ],
        )

    def test_local_kubernetes_manager_does_not_request_in_cluster_mode(self):
        for engine in ("wasabi", "joinmarket"):
            with (
                self.subTest(engine=engine),
                mock.patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.43.0.1"}),
            ):
                command = kubernetes_emulator_command(Namespace(engine=engine), "/scenario.json")
            self.assertNotIn("--in-cluster", command)

    def test_kubernetes_emulator_command_can_copy_btc_data_to_host(self):
        command = kubernetes_emulator_command(
            Namespace(copy_to_host=True),
            "/app/scenarios/overactive-local.json",
            btc_data_path="/btc-data/custom",
        )

        self.assertIn("--download-btc-data", command)
        self.assertEqual(
            command[command.index("--download-btc-data") + 1],
            "/btc-data/custom",
        )
        self.assertNotIn("--btcFolder", command)

    def test_kubernetes_emulator_command_accepts_control_ip(self):
        command = kubernetes_emulator_command(
            Namespace(engine="joinmarket"),
            "/app/scenarios/overactive-local.json",
            control_ip="172.17.0.1",
        )

        self.assertEqual(command[command.index("--engine") + 1], "joinmarket")
        self.assertEqual(command[command.index("--run-timezone") + 1], "Europe/Prague")
        self.assertIn("--control-ip", command)
        self.assertEqual(command[command.index("--control-ip") + 1], "172.17.0.1")
        self.assertNotIn("--joinmarket-descriptor-regtest-fallback", command)

    def test_kubernetes_emulator_command_omits_obsolete_fallback_for_wasabi(self):
        command = kubernetes_emulator_command(Namespace(engine="wasabi"), "/app/scenarios/overactive-local.json")

        self.assertNotIn("--joinmarket-descriptor-regtest-fallback", command)

    def test_kubernetes_emulator_command_can_reuse_namespace(self):
        command = kubernetes_emulator_command(Namespace(reuse_namespace=True), "/app/scenarios/overactive-local.json")

        self.assertEqual(command[-1], "--reuse-namespace")

    def test_kubernetes_emulator_command_can_request_local_build(self):
        command = kubernetes_emulator_command(
            Namespace(engine="joinmarket"),
            "/app/scenarios/defaultJoinMarket.json",
            local_build=True,
        )

        self.assertIn("--coinjoin-infrastructure-local-build", command)
        self.assertNotIn("--btc-node-image", command)
        self.assertNotIn("--joinmarket-client-server-image", command)
        self.assertNotIn("--irc-server-image", command)

    def test_kubernetes_emulator_command_passes_btc_node_image_override(self):
        with mock.patch.dict(os.environ, {"COINJOIN_BTC_NODE_IMAGE": "btc-node:test"}, clear=False):
            command = kubernetes_emulator_command(Namespace(), "/app/scenarios/overactive-local.json")

        self.assertEqual(command[command.index("--btc-node-image") + 1], "btc-node:test")

    def test_kubernetes_auth_preflight_checks_owned_namespace_permissions(self):
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "yes\n")

        with mock.patch(
            "coinjoin_pipeline.execution.kubernetes.subprocess.run",
            side_effect=fake_run,
        ):
            kubernetes_auth_preflight(Path("/kube/config"), "coinjoin-test", reuse_namespace=False)

        self.assertEqual(
            calls,
            [
                _kubectl_cmd("get", "--raw=/version"),
                _kubectl_cmd("auth", "can-i", "create", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd(
                    "auth",
                    "can-i",
                    "create",
                    "services",
                    "--namespace",
                    "coinjoin-test",
                ),
                _kubectl_cmd("auth", "can-i", "delete", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd(
                    "auth",
                    "can-i",
                    "delete",
                    "services",
                    "--namespace",
                    "coinjoin-test",
                ),
                _kubectl_cmd("auth", "can-i", "create", "namespaces"),
                _kubectl_cmd("auth", "can-i", "delete", "namespaces"),
            ],
        )

    def test_kubernetes_auth_preflight_checks_reused_namespace_permissions(self):
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "yes\n")

        with mock.patch(
            "coinjoin_pipeline.execution.kubernetes.subprocess.run",
            side_effect=fake_run,
        ):
            kubernetes_auth_preflight(Path("/kube/config"), "coinjoin-test", reuse_namespace=True)

        self.assertEqual(
            calls,
            [
                _kubectl_cmd("get", "--raw=/version"),
                _kubectl_cmd("auth", "can-i", "create", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd(
                    "auth",
                    "can-i",
                    "create",
                    "services",
                    "--namespace",
                    "coinjoin-test",
                ),
                _kubectl_cmd("auth", "can-i", "delete", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd(
                    "auth",
                    "can-i",
                    "delete",
                    "services",
                    "--namespace",
                    "coinjoin-test",
                ),
                _kubectl_cmd("get", "namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "list", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "list", "services", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "delete", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd(
                    "auth",
                    "can-i",
                    "delete",
                    "services",
                    "--namespace",
                    "coinjoin-test",
                ),
            ],
        )

    def test_kubernetes_auth_preflight_stops_on_denied_permission(self):
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            output = "no\n" if command[3:7] == ["auth", "can-i", "create", "pods"] else "yes\n"
            return subprocess.CompletedProcess(command, 0, output)

        with mock.patch(
            "coinjoin_pipeline.execution.kubernetes.subprocess.run",
            side_effect=fake_run,
        ):
            with self.assertRaises(SystemExit) as context:
                kubernetes_auth_preflight(Path("/kube/config"), "coinjoin-test", reuse_namespace=False)

        self.assertEqual(context.exception.code, 2)
        self.assertEqual(
            calls[-1],
            _kubectl_cmd("auth", "can-i", "create", "pods", "--namespace", "coinjoin-test"),
        )

    def test_kubernetes_auth_preflight_tolerates_kubectl_namespace_warning(self):
        """kubectl >= 1.35 emits 'Warning: resource 'namespaces' is not namespace
        scoped' to stderr when checking cluster-scoped resources like namespaces.
        The preflight must not mistake this warning for a denied permission."""

        def fake_run(command, **_kwargs):
            if command[3:7] == ["auth", "can-i", "create", "namespaces"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="yes\n",
                    stderr="Warning: resource 'namespaces' is not namespace scoped\n",
                )
            if command[3:7] == ["auth", "can-i", "delete", "namespaces"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="yes\n",
                    stderr="Warning: resource 'namespaces' is not namespace scoped\n",
                )
            return subprocess.CompletedProcess(command, 0, stdout="yes\n", stderr="")

        with mock.patch(
            "coinjoin_pipeline.execution.kubernetes.subprocess.run",
            side_effect=fake_run,
        ):
            kubernetes_auth_preflight(Path("/kube/config"), "coinjoin-test", reuse_namespace=False)

    def test_compose_manager_command_sets_default_emulator_image_prefix(self):
        compose_yaml = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn(
            'python manager.py --engine ${COINJOIN_ENGINE:-wasabi} --run-timezone \\"$${RUN_TIMEZONE}\\" run '
            '$${PIPELINE_RUN_ID:+--run-id \\"$${PIPELINE_RUN_ID}\\"} '
            "${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:+--coinjoin-infrastructure-local-build}",
            compose_yaml,
        )
        self.assertIn("RUN_TIMEZONE=${RUN_TIMEZONE:-Europe/Prague}", compose_yaml)
        self.assertIn(
            "--image-prefix ${COINJOIN_EMULATOR_IMAGE_PREFIX:-ghcr.io/ondrejman/}",
            compose_yaml,
        )
        self.assertNotIn(
            "--btc-node-image",
            compose_yaml,
        )
        self.assertNotIn(
            "--joinmarket-client-server-image",
            compose_yaml,
        )
        self.assertNotIn(
            "--irc-server-image",
            compose_yaml,
        )
        self.assertIn(
            "${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:+--coinjoin-infrastructure-local-build}",
            compose_yaml,
        )
        self.assertIn("Skipping btc-node pull; local build requested", compose_yaml)
        self.assertIn(
            "Skipping JoinMarket base pull; the manager builds it from the vendored source",
            compose_yaml,
        )
        self.assertNotIn("pull_image ghcr.io/ondrejman/joinmarket-base", compose_yaml)
        self.assertIn("Skipping irc-server pull; local build requested", compose_yaml)
        self.assertIn("--download-btc-data /home/bitcoin/data", compose_yaml)

    def test_compose_prefetch_uses_prefix_derived_refs(self):
        compose_yaml = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("PREFIX=${COINJOIN_EMULATOR_IMAGE_PREFIX:-ghcr.io/ondrejman/}", compose_yaml)
        self.assertIn("$${PREFIX}btc-node", compose_yaml)
        self.assertIn("$${PREFIX}joinmarket-client-server", compose_yaml)
        self.assertIn("$${PREFIX}irc-server:latest", compose_yaml)
        # No per-image env vars in prefetch
        self.assertNotIn("COINJOIN_EMULATOR_BTC_NODE_IMAGE", compose_yaml)
        self.assertNotIn("COINJOIN_EMULATOR_JOINMARKET_CLIENT_SERVER_IMAGE", compose_yaml)
        self.assertNotIn("COINJOIN_EMULATOR_IRC_SERVER_IMAGE", compose_yaml)

    def test_kubernetes_emulation_mounts_scenarios_at_container_scenario_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            checkout_dir = root / "checkout"
            scenarios_dir = checkout_dir / "scenarios"
            kubeconfig = root / "kubeconfig.yaml"
            scenarios_dir.mkdir(parents=True)
            kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

            env = {
                "SCENARIOS_DIR": str(checkout_dir / "scenarios"),
                "EMULATION_LOGS_DIR": str(root / "logs"),
                "CONTAINER_RUNTIME": "podman",
                "COINJOIN_EMULATOR_IMAGE": "coinjoin-emulator:test",
                "KUBERNETES_STORAGE_UID": "1234",
                "KUBERNETES_STORAGE_GID": "5678",
                "KUBERNETES_IMAGE_PULL_POLICY": "IfNotPresent",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("coinjoin_pipeline.execution.containers.run_command") as run_mock,
                mock.patch("coinjoin_pipeline.execution.kubernetes_launch.populate_btc_data_volume") as populate_mock,
                mock.patch("coinjoin_pipeline.execution.kubernetes_launch.kubernetes_auth_preflight"),
            ):
                run_kubernetes_emulation(
                    Namespace(
                        scenario="overactive-local.json",
                        namespace="coinjoin-test",
                        kubeconfig=str(kubeconfig),
                        run_timezone="UTC",
                    ),
                    btc_datadir=str(root / "btc-data/data"),
                )

            docker_cmd = run_mock.call_args.args[0]
            self.assertIn("coinjoin-emulator:test", docker_cmd)
            self.assertIn("--pull=missing", docker_cmd)
            self.assertEqual(docker_cmd[docker_cmd.index("--user") + 1], "1234:5678")
            self.assertIn(f"{kubeconfig.resolve()}:/tmp/coinjoin-kubeconfig:ro", docker_cmd)
            self.assertIn("HOME=/tmp", docker_cmd)
            self.assertIn("KUBECONFIG=/tmp/coinjoin-kubeconfig", docker_cmd)
            self.assertIn(f"{scenarios_dir.resolve()}:/mnt/scenarios:ro", docker_cmd)
            self.assertIn("/mnt/scenarios/overactive-local.json", docker_cmd)
            self.assertIn("--control-ip", docker_cmd)
            self.assertIn("host.docker.internal", docker_cmd)
            self.assertEqual(docker_cmd[docker_cmd.index("--run-timezone") + 1], "UTC")
            self.assertIn("--btcFolder", docker_cmd)
            self.assertEqual(
                docker_cmd[docker_cmd.index("--btcFolder") + 1],
                str((root / "btc-data" / "data").resolve()),
            )
            self.assertNotIn("--download-btc-data", docker_cmd)
            self.assertNotIn(f"{(root / 'btc-data').resolve()}:/btc-data:rw", docker_cmd)
            self.assertIn("KUBERNETES_STORAGE_UID=1234", docker_cmd)
            self.assertIn("KUBERNETES_STORAGE_GID=5678", docker_cmd)
            self.assertIn("KUBERNETES_IMAGE_PULL_POLICY=IfNotPresent", docker_cmd)
            populate_mock.assert_called_once_with((root / "btc-data" / "data").resolve())
            self.assertNotIn(f"{scenarios_dir.resolve()}:/app/scenarios:ro", docker_cmd)

    def test_kubernetes_emulation_uses_requested_container_network(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            checkout_dir = root / "checkout"
            scenarios_dir = checkout_dir / "scenarios"
            kubeconfig = root / "kubeconfig.yaml"
            scenarios_dir.mkdir(parents=True)
            kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

            env = {
                "SCENARIOS_DIR": str(checkout_dir / "scenarios"),
                "EMULATION_LOGS_DIR": str(root / "logs"),
                "CONTAINER_RUNTIME": "docker",
                "COINJOIN_EMULATOR_IMAGE": "coinjoin-emulator:test",
                "KUBERNETES_EMULATOR_CONTAINER_NETWORK": "k3d-coinjoin-test",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("coinjoin_pipeline.execution.containers.run_command") as run_mock,
                mock.patch("coinjoin_pipeline.execution.kubernetes_launch.populate_btc_data_volume"),
                mock.patch("coinjoin_pipeline.execution.kubernetes_launch.kubernetes_auth_preflight"),
            ):
                run_kubernetes_emulation(
                    Namespace(scenario="overactive-local.json", namespace="coinjoin-test", kubeconfig=str(kubeconfig))
                )

            docker_cmd = run_mock.call_args.args[0]
            self.assertEqual(
                docker_cmd[docker_cmd.index("--network") + 1],
                "k3d-coinjoin-test",
            )

    def test_kubernetes_emulation_copy_to_host_preserves_download_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            checkout_dir = root / "checkout"
            scenarios_dir = checkout_dir / "scenarios"
            kubeconfig = root / "kubeconfig.yaml"
            scenarios_dir.mkdir(parents=True)
            kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

            env = {
                "SCENARIOS_DIR": str(checkout_dir / "scenarios"),
                "EMULATION_LOGS_DIR": str(root / "logs"),
                "CONTAINER_RUNTIME": "podman",
                "COINJOIN_EMULATOR_IMAGE": "coinjoin-emulator:test",
                "KUBERNETES_COPY_TO_HOST_DIR": str(root / "kubernetes-download"),
                "KUBERNETES_STORAGE_UID": "1234",
                "KUBERNETES_STORAGE_GID": "5678",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch("coinjoin_pipeline.execution.containers.run_command") as run_mock,
                mock.patch("coinjoin_pipeline.execution.kubernetes_launch.populate_btc_data_volume") as populate_mock,
                mock.patch("coinjoin_pipeline.execution.kubernetes_launch.kubernetes_auth_preflight"),
            ):
                run_kubernetes_emulation(
                    Namespace(scenario="overactive-local.json", kubeconfig=str(kubeconfig), copy_to_host=True)
                )

            docker_cmd = run_mock.call_args.args[0]
            self.assertEqual(
                docker_cmd[docker_cmd.index("--user") + 1],
                "1234:5678",
            )
            self.assertIn(f"{kubeconfig.resolve()}:/tmp/coinjoin-kubeconfig:ro", docker_cmd)
            self.assertIn("--download-btc-data", docker_cmd)
            self.assertEqual(
                docker_cmd[docker_cmd.index("--download-btc-data") + 1],
                "/btc-data/data",
            )
            self.assertIn(f"{(root / 'kubernetes-download').resolve()}:/btc-data:rw", docker_cmd)
            populate_mock.assert_called_once_with((root / "kubernetes-download" / "data").resolve())

    def test_kubernetes_emulation_copy_to_host_requires_explicit_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            checkout_dir = root / "checkout"
            scenarios_dir = checkout_dir / "scenarios"
            kubeconfig = root / "kubeconfig.yaml"
            scenarios_dir.mkdir(parents=True)
            kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

            env = {
                "SCENARIOS_DIR": str(checkout_dir / "scenarios"),
                "EMULATION_LOGS_DIR": str(root / "logs"),
                "CONTAINER_RUNTIME": "podman",
                "COINJOIN_EMULATOR_IMAGE": "coinjoin-emulator:test",
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                self.assertRaises(SystemExit) as error,
            ):
                run_kubernetes_emulation(
                    Namespace(scenario="overactive-local.json", kubeconfig=str(kubeconfig), copy_to_host=True)
                )

            self.assertEqual(error.exception.code, 2)

    def test_container_run_pull_args_default_to_always_for_registry_images(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                container_run_pull_args(
                    "ghcr.io/ondrejman/emulator-manager:latest",
                    "COINJOIN_EMULATOR_PULL_POLICY",
                ),
                ["--pull=always"],
            )

    def test_container_run_pull_args_default_to_missing_for_local_tags(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                container_run_pull_args("coinjoin-emulator:test", "COINJOIN_EMULATOR_PULL_POLICY"),
                ["--pull=missing"],
            )

    def test_container_run_pull_args_honor_env_override(self):
        with mock.patch.dict(os.environ, {"COINJOIN_EMULATOR_PULL_POLICY": "never"}, clear=True):
            self.assertEqual(
                container_run_pull_args(
                    "ghcr.io/ondrejman/emulator-manager:latest",
                    "COINJOIN_EMULATOR_PULL_POLICY",
                ),
                ["--pull=never"],
            )


class RunDirResolutionTests(unittest.TestCase):
    def test_typoed_run_dir_is_rejected_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            with self.assertRaises(SystemExit):
                run_dir_under_root("does-not-exist", runs_root)
            self.assertFalse((runs_root / "does-not-exist").exists())

    def test_absolute_run_dir_outside_runs_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_root = root / "runs"
            runs_root.mkdir()
            outside = root / "archive" / "2026-07-09_08-14_default"
            outside.mkdir(parents=True)
            with self.assertRaises(SystemExit):
                run_dir_under_root(str(outside), runs_root)

    def test_valid_run_dir_resolves_under_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            (runs_root / "2026-07-09_08-14_default").mkdir()
            resolved = run_dir_under_root("2026-07-09_08-14_default", runs_root)
            self.assertEqual(resolved.parent, runs_root)
            self.assertEqual(resolved.name, "2026-07-09_08-14_default")


class ScenarioPathTests(unittest.TestCase):
    def test_scenario_outside_scenarios_dir_is_a_hard_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenarios_dir = root / "scenarios"
            scenarios_dir.mkdir()
            outside = root / "experiments" / "wasabi-40.json"
            outside.parent.mkdir(parents=True)
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaises(SystemExit):
                container_scenario_path(str(outside), scenarios_dir)

    def test_nested_scenario_round_trips_without_flattening(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenarios_dir = Path(tmp) / "scenarios"
            (scenarios_dir / "sub").mkdir(parents=True)
            (scenarios_dir / "sub" / "x.json").write_text("{}", encoding="utf-8")
            container = container_scenario_path("scenarios/sub/x.json", scenarios_dir)
            self.assertEqual(container, "/mnt/scenarios/sub/x.json")
            self.assertEqual(
                host_scenario_path(container, scenarios_dir),
                scenarios_dir / "sub" / "x.json",
            )


if __name__ == "__main__":
    unittest.main()
