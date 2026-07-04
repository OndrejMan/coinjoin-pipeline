import io
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import ArgumentTypeError, Namespace
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from client.wrapper import (
    COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV,
    COINJOIN_ANALYSIS_MOUNT_PATH_ENV,
    COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER,
    COINJOIN_ANALYSIS_SOURCE_PATH_ENV,
    COINJOIN_ANALYSIS_TARGET_PATH_ENV,
    DEFAULT_RUN_TIMEZONE,
    RUNS_ROOT_CONTAINER,
    blocksci_output_exists,
    captured_pipeline_stage,
    compose_command,
    compose_env,
    container_command,
    container_run_pull_args,
    container_runtime,
    export_command,
    export_preflight_error,
    kubernetes_auth_preflight,
    kubernetes_emulator_command,
    normalize_argv,
    pipeline_stage,
    run_blocksci_docker_stage,
    run_coinjoin_analysis,
    run_command,
    run_dirs,
    run_kubernetes_emulation,
    run_parallel_analysis,
    run_timezone,
    stage_blocksci_script,
    stage_separator,
    terminal_supports_color,
)


def _kubectl_cmd(*parts: str) -> list[str]:
    return ["kubectl", "--kubeconfig", "/kube/config", *parts]


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

    def test_wrapper_image_includes_pbs_module(self):
        dockerfile = (PROJECT_ROOT / "client" / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY client/pbs.py /app/client/pbs.py", dockerfile)

    def test_wrapper_image_includes_refactored_client_modules(self):
        dockerfile = (PROJECT_ROOT / "client" / "Dockerfile").read_text(encoding="utf-8")

        for module in ("cli_options.py", "kubernetes.py", "pipeline_logging.py", "runtime.py"):
            self.assertIn(f"COPY client/{module} /app/client/{module}", dockerfile)

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

        with mock.patch("client.wrapper.sys.stdout", stream):
            with pipeline_stage("Example stage"):
                pass

        output = stream.getvalue()
        self.assertIn("[pipeline] START: Example stage", output)
        self.assertIn("[pipeline] DONE: Example stage", output)
        self.assertIn(("=" * 88) + "\n" + ("=" * 88) + "\n" + ("=" * 88), output)

    def test_captured_pipeline_stage_writes_merged_run_log_and_keeps_terminal_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "run-a"
            terminal = io.StringIO()

            with mock.patch("client.wrapper.sys.stdout", terminal), mock.patch("client.wrapper.sys.stderr", terminal):
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
            self.assertIn("[pipeline] FAILED: Docker emulation", failed_logs[0].read_text(encoding="utf-8"))

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
                run_command([
                    sys.executable,
                    "-c",
                    "import sys; print('child stdout'); print('child stderr', file=sys.stderr)",
                ])

            log_text = stage_log.path.read_text(encoding="utf-8")
            self.assertIn("child stdout", log_text)
            self.assertIn("child stderr", log_text)

    def test_normalize_argv_defaults_to_full_run(self):
        self.assertEqual(
            normalize_argv(["--scenario", "overactive-local.json"]),
            ["full-run", "--scenario", "overactive-local.json"],
        )

    def test_normalize_argv_defaults_to_full_run_with_test_values(self):
        self.assertEqual(
            normalize_argv(["--test-values", "--scenario", "overactive-local.json"]),
            ["full-run", "--test-values", "--scenario", "overactive-local.json"],
        )

    def test_normalize_argv_defaults_to_full_run_with_parallel(self):
        self.assertEqual(
            normalize_argv(["--parallel", "--engine", "joinmarket"]),
            ["full-run", "--parallel", "--engine", "joinmarket"],
        )

    def test_full_run_accepts_parallel_flag(self):
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "client" / "wrapper.py"),
                "full-run",
                "--engine",
                "joinmarket",
                "--parallel",
                "--dry-run",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

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
                test_values=False,
                joinmarket_detector="definite",
                joinmarket_min_base_fee=5000,
                joinmarket_percentage_fee=0.00004,
                joinmarket_max_depth=200000,
            )
            with mock.patch("client.wrapper.run_command") as run_mock:
                run_blocksci_docker_stage(args, run_dir, include_report=False)

            command = run_mock.call_args.args[0]
            self.assertIn("--no-deps", command)
            self.assertEqual(command[-1], "blocksci")
            self.assertEqual(run_mock.call_args.kwargs["env"]["BLOCKSCI_EXPORT_REPORT"], "false")

    def test_parallel_analysis_supports_all_docker_pbs_combinations(self):
        combinations = ((False, False), (True, True), (False, True), (True, False))
        for analysis_pbs, blocksci_pbs in combinations:
            with self.subTest(analysis_pbs=analysis_pbs, blocksci_pbs=blocksci_pbs):
                with tempfile.TemporaryDirectory() as tmpdir:
                    logs_root = Path(tmpdir)
                    run_dir = logs_root / "run-a"
                    run_dir.mkdir()
                    args = Namespace(
                        analysisPbs=analysis_pbs,
                        blocksciPbs=blocksci_pbs,
                    )
                    with mock.patch("client.wrapper.run_coinjoin_analysis_pbs_stage") as coinjoin_pbs_mock, \
                         mock.patch("client.wrapper.run_blocksci_pbs_stage") as blocksci_pbs_mock, \
                         mock.patch("client.wrapper.wait_for_pbs_marker"), \
                         mock.patch("client.wrapper.run_coinjoin_analysis_docker_stage") as coinjoin_docker_mock, \
                         mock.patch("client.wrapper.run_blocksci_docker_stage") as blocksci_docker_mock, \
                         mock.patch("client.wrapper.run_blocksci_export_pbs_stage") as pbs_export_mock, \
                         mock.patch("client.wrapper.run_export_only") as docker_export_mock:
                        run_parallel_analysis(args, run_dir, logs_root)

                    self.assertEqual(coinjoin_pbs_mock.called, analysis_pbs)
                    self.assertEqual(coinjoin_docker_mock.called, not analysis_pbs)
                    self.assertEqual(blocksci_pbs_mock.called, blocksci_pbs)
                    self.assertEqual(blocksci_docker_mock.called, not blocksci_pbs)
                    self.assertEqual(pbs_export_mock.called, blocksci_pbs)
                    self.assertEqual(docker_export_mock.called, not blocksci_pbs)

    def test_parallel_analysis_waits_for_both_and_skips_export_on_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_root = Path(tmpdir)
            run_dir = logs_root / "run-a"
            run_dir.mkdir()
            args = Namespace(analysisPbs=False, blocksciPbs=False)
            completed = []

            def fail_coinjoin(_run_id):
                completed.append("coinjoin")
                raise subprocess.CalledProcessError(7, ["coinjoin_analysis"])

            def finish_blocksci(_args, _run_dir, *, include_report):
                self.assertFalse(include_report)
                completed.append("blocksci")

            with mock.patch("client.wrapper.run_coinjoin_analysis_docker_stage", side_effect=fail_coinjoin), \
                 mock.patch("client.wrapper.run_blocksci_docker_stage", side_effect=finish_blocksci), \
                 mock.patch("client.wrapper.run_export_only") as export_mock:
                with self.assertRaisesRegex(RuntimeError, "coinjoin-analysis"):
                    run_parallel_analysis(args, run_dir, logs_root)

            self.assertCountEqual(completed, ["coinjoin", "blocksci"])
            export_mock.assert_not_called()

    def test_parallel_analysis_runs_mappings_after_baseline_before_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_root = Path(tmpdir)
            run_dir = logs_root / "run-a"
            run_dir.mkdir()
            args = Namespace(analysisPbs=False, blocksciPbs=False, mappingsPbs=True)
            events = []
            with mock.patch(
                "client.wrapper.run_coinjoin_analysis_docker_stage",
                side_effect=lambda _run_id: events.append("baseline"),
            ), mock.patch(
                "client.wrapper.run_blocksci_docker_stage",
                side_effect=lambda *_args, **_kwargs: events.append("blocksci"),
            ), mock.patch(
                "client.wrapper.run_mappings_pbs_stage",
                side_effect=lambda *_args: events.append("mappings"),
            ), mock.patch(
                "client.wrapper.run_export_only",
                side_effect=lambda *_args: events.append("export"),
            ):
                run_parallel_analysis(args, run_dir, logs_root)

            self.assertLess(events.index("baseline"), events.index("mappings"))
            self.assertLess(events.index("mappings"), events.index("export"))

    def test_normalize_argv_keeps_explicit_action(self):
        self.assertEqual(
            normalize_argv(["recreate", "--scenario", "overactive-local.json"]),
            ["recreate", "--scenario", "overactive-local.json"],
        )

    def test_normalize_argv_keeps_help_without_default_action(self):
        self.assertEqual(normalize_argv(["--help"]), ["--help"])

    def test_wrapper_help_exits_successfully(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "client" / "wrapper.py"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("full-run", result.stdout)

    def test_full_run_requires_explicit_engine(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "client" / "wrapper.py"), "full-run"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--engine", result.stderr)

    def test_mappings_pbs_is_rejected_for_unrelated_action(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "client" / "wrapper.py"), "analyze",
             "--engine", "wasabi", "--run-dir", "run-a", "--mappingsPbs"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("supported only by full-run and mappings", result.stderr)

    def test_mappings_pbs_requires_wasabi2_coinjoin_type(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "client" / "wrapper.py"), "mappings",
             "--engine", "wasabi", "--coinjoin-type", "joinmarket", "--run-dir", "run-a",
             "--mappingsPbs", "--dry-run"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --coinjoin-type wasabi2", result.stderr)

    def test_dry_run_does_not_start_pipeline(self):
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "client" / "wrapper.py"),
                "full-run",
                "--engine",
                "joinmarket",
                "--dry-run",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("[dry-run] action: full-run", result.stdout)
        self.assertIn("No containers", result.stdout)

    def test_clean_requires_explicit_confirmation(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "client" / "wrapper.py"), "clean"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--yes", result.stderr)

    def test_container_runtime_defaults_to_docker(self):
        self.assertEqual(container_runtime({}), "docker")
        self.assertEqual(container_command({}), ["docker"])
        self.assertEqual(compose_command({}), ["docker", "compose"])

    def test_container_runtime_supports_podman(self):
        env = {"CONTAINER_RUNTIME": "podman"}

        self.assertEqual(container_runtime(env), "podman")
        self.assertEqual(container_command(env), ["podman"])
        self.assertEqual(compose_command(env), ["podman", "compose"])

    def test_compose_command_can_be_overridden(self):
        env = {
            "CONTAINER_RUNTIME": "podman",
            "CONTAINER_COMPOSE_COMMAND": "podman-compose",
        }

        self.assertEqual(compose_command(env), ["podman-compose"])

    def test_compose_env_sets_test_values_flag(self):
        env = compose_env(test_values=True)

        self.assertEqual(env["BLOCKSCI_TEST_VALUES"], "true")

    def test_compose_env_sets_default_run_timezone(self):
        self.assertEqual(compose_env()["RUN_TIMEZONE"], DEFAULT_RUN_TIMEZONE)

    def test_compose_env_allows_run_timezone_override(self):
        self.assertEqual(compose_env(run_timezone_name="UTC")["RUN_TIMEZONE"], "UTC")

    def test_run_timezone_rejects_unknown_iana_name(self):
        with self.assertRaises(ArgumentTypeError):
            run_timezone("not/a-timezone")

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
            logs_dir = Path(tmpdir) / "bitcoinAnalysis" / "emulation_logs"
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

    def test_export_command_includes_test_values_when_enabled(self):
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

        command = export_command("run-a", env)

        self.assertIn("--test-values", command)
        self.assertIn("--markdown", command)
        self.assertIn("--joinmarket-detector", command)
        self.assertIn(f"{RUNS_ROOT_CONTAINER}/run-a/blocksci_data/config.json", command)
        self.assertIn(f"{RUNS_ROOT_CONTAINER}/run-a", command)

    def test_export_command_omits_test_values_by_default(self):
        env = {
            "SCENARIO_FALLBACK_PATH": "/mnt/scenarios/defaultCoinJoin.json",
            "BLOCKSCI_COINJOIN_TYPE": "wasabi2",
            "BLOCKSCI_MIN_INPUT_COUNT": "1",
            "BLOCKSCI_TEST_VALUES": "false",
            "BLOCKSCI_JOINMARKET_DETECTOR": "definite",
            "BLOCKSCI_JOINMARKET_MIN_BASE_FEE": "5000",
            "BLOCKSCI_JOINMARKET_PERCENTAGE_FEE": "0.00004",
            "BLOCKSCI_JOINMARKET_MAX_DEPTH": "200000",
        }

        self.assertNotIn("--test-values", export_command("run-a", env))

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
        self.assertIn("CoinJoin output exists", error)
        self.assertIn("BlockSci run output is missing", error)
        self.assertIn("python3 client/wrapper.py analyze --run-dir 2026-05-24_16-58_default", error)

    def test_export_preflight_blocksci_only(self):
        error = export_preflight_error(
            coinjoin_ready=False,
            blocksci_ready=True,
            run_dir=Path("/tmp/2026-05-24_16-58_default"),
        )

        assert error is not None
        self.assertIn("BlockSci run output exists", error)
        self.assertIn("CoinJoin output is missing", error)
        self.assertIn(
            "python3 client/wrapper.py coinjoin-analysis --run-dir 2026-05-24_16-58_default",
            error,
        )

    def test_export_preflight_neither_ready(self):
        error = export_preflight_error(
            coinjoin_ready=False,
            blocksci_ready=False,
            run_dir=Path("/tmp/2026-05-24_16-58_default"),
        )

        assert error is not None
        self.assertIn("neither prerequisite is ready", error)
        self.assertIn("Missing CoinJoin output", error)
        self.assertIn("Missing BlockSci run output", error)

    def test_blocksci_output_requires_run_local_parsed_chain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            blocksci_data = run_dir / "blocksci_data"
            blocksci_data.mkdir(parents=True)
            (blocksci_data / "config.json").write_text("{}", encoding="utf-8")

            self.assertFalse(blocksci_output_exists(run_dir))

            parsed_chain = blocksci_data / "parsed" / "chain"
            parsed_chain.mkdir(parents=True)
            (parsed_chain / "block.dat").write_bytes(b"parsed chain")

            self.assertTrue(blocksci_output_exists(run_dir))

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

    def test_run_coinjoin_analysis_targets_requested_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            host_client_dir = root / "client"
            run_dir = root / "emulation_logs" / "run-a"
            host_client_dir.mkdir()
            run_dir.mkdir(parents=True)
            (run_dir / "coinjoin_emulator_data" / "data").mkdir(parents=True)
            (run_dir / "coinjoin_emulator_data" / "scenario.json").write_text("{}", encoding="utf-8")

            env = {
                "HOST_CLIENT_DIR": str(host_client_dir),
                "CONTAINER_RUNTIME": "docker",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("client.wrapper.run_command") as run_mock:
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
            host_client_dir = root / "client"
            host_client_dir.mkdir()
            for run_id in ("run-a", "run-b"):
                run_dir = root / "emulation_logs" / run_id / "coinjoin_emulator_data"
                (run_dir / "data").mkdir(parents=True)
                (run_dir / "scenario.json").write_text("{}", encoding="utf-8")

            env = {
                "HOST_CLIENT_DIR": str(host_client_dir),
                "CONTAINER_RUNTIME": "docker",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("client.wrapper.run_command") as run_mock:
                run_coinjoin_analysis(all_runs=True)

            compose_calls = [
                call
                for call in run_mock.call_args_list
                if "coinjoin_analysis" in call.args[0]
            ]
            self.assertEqual(len(compose_calls), 2)
            analysis_sources = {
                call.kwargs["env"][COINJOIN_ANALYSIS_SOURCE_PATH_ENV]
                for call in compose_calls
            }
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
            host_client_dir = root / "client"
            run_dir = root / "emulation_logs" / "run-a"
            host_client_dir.mkdir()
            (run_dir / "coinjoin_emulator_data" / "data").mkdir(parents=True)
            analysis_dir = run_dir / "coinjoin-analysis_data"
            analysis_dir.mkdir()
            (analysis_dir / "coinjoin_tx_info.json").write_text("{}", encoding="utf-8")

            env = {"HOST_CLIENT_DIR": str(host_client_dir), "CONTAINER_RUNTIME": "docker"}
            with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("client.wrapper.run_command") as run_mock:
                run_coinjoin_analysis("run-a", analysis_action="analyze_only")

            self.assertEqual(
                run_mock.call_args.kwargs["env"]["COINJOIN_ANALYSIS_ACTION"],
                "analyze_only",
            )

    def test_run_coinjoin_analysis_analyze_only_requires_existing_baseline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            host_client_dir = root / "client"
            run_dir = root / "emulation_logs" / "run-a"
            host_client_dir.mkdir()
            (run_dir / "coinjoin_emulator_data" / "data").mkdir(parents=True)
            (run_dir / "coinjoin_emulator_data" / "scenario.json").write_text("{}", encoding="utf-8")

            env = {"HOST_CLIENT_DIR": str(host_client_dir), "CONTAINER_RUNTIME": "docker"}
            with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("client.wrapper.run_command") as run_mock, \
                self.assertRaises(SystemExit) as raised:
                run_coinjoin_analysis("run-a", analysis_action="analyze_only")

            self.assertEqual(raised.exception.code, 2)
            run_mock.assert_not_called()

    def test_kubernetes_emulator_command_places_driver_before_subcommand(self):
        command = kubernetes_emulator_command(
            scenario="/app/scenarios/overactive-local.json",
            namespace="coinjoin-test",
            image_prefix="ghcr.io/test/",
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

    def test_kubernetes_emulator_command_can_copy_btc_data_to_host(self):
        command = kubernetes_emulator_command(
            scenario="/app/scenarios/overactive-local.json",
            btc_data_path="/btc-data/custom",
            copy_to_host=True,
        )

        self.assertIn("--download-btc-data", command)
        self.assertEqual(
            command[command.index("--download-btc-data") + 1],
            "/btc-data/custom",
        )
        self.assertNotIn("--btcFolder", command)

    def test_kubernetes_emulator_command_accepts_control_ip(self):
        command = kubernetes_emulator_command(
            scenario="/app/scenarios/overactive-local.json",
            engine="joinmarket",
            control_ip="172.17.0.1",
        )

        self.assertEqual(command[command.index("--engine") + 1], "joinmarket")
        self.assertEqual(command[command.index("--run-timezone") + 1], "Europe/Prague")
        self.assertIn("--control-ip", command)
        self.assertEqual(command[command.index("--control-ip") + 1], "172.17.0.1")

    def test_kubernetes_emulator_command_can_reuse_namespace(self):
        command = kubernetes_emulator_command(
            scenario="/app/scenarios/overactive-local.json",
            reuse_namespace=True,
        )

        self.assertEqual(command[-1], "--reuse-namespace")

    def test_kubernetes_emulator_command_can_request_local_build(self):
        command = kubernetes_emulator_command(
            scenario="/app/scenarios/defaultJoinMarket.json",
            engine="joinmarket",
            coinjoin_infrastructure_local_build=True,
        )

        self.assertIn("--coinjoin-infrastructure-local-build", command)
        self.assertNotIn("--btc-node-image", command)
        self.assertNotIn("--joinmarket-client-server-image", command)
        self.assertNotIn("--irc-server-image", command)

    def test_kubernetes_auth_preflight_checks_owned_namespace_permissions(self):
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "yes\n")

        with mock.patch("client.kubernetes.subprocess.run", side_effect=fake_run):
            kubernetes_auth_preflight(Path("/kube/config"), "coinjoin-test", reuse_namespace=False)

        self.assertEqual(
            calls,
            [
                _kubectl_cmd("get", "--raw=/version"),
                _kubectl_cmd("auth", "can-i", "create", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "create", "services", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "delete", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "delete", "services", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "create", "namespaces"),
                _kubectl_cmd("auth", "can-i", "delete", "namespaces"),
            ],
        )

    def test_kubernetes_auth_preflight_checks_reused_namespace_permissions(self):
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "yes\n")

        with mock.patch("client.kubernetes.subprocess.run", side_effect=fake_run):
            kubernetes_auth_preflight(Path("/kube/config"), "coinjoin-test", reuse_namespace=True)

        self.assertEqual(
            calls,
            [
                _kubectl_cmd("get", "--raw=/version"),
                _kubectl_cmd("auth", "can-i", "create", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "create", "services", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "delete", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "delete", "services", "--namespace", "coinjoin-test"),
                _kubectl_cmd("get", "namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "list", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "list", "services", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "delete", "pods", "--namespace", "coinjoin-test"),
                _kubectl_cmd("auth", "can-i", "delete", "services", "--namespace", "coinjoin-test"),
            ],
        )

    def test_kubernetes_auth_preflight_stops_on_denied_permission(self):
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            output = "no\n" if command[3:7] == ["auth", "can-i", "create", "pods"] else "yes\n"
            return subprocess.CompletedProcess(command, 0, output)

        with mock.patch("client.kubernetes.subprocess.run", side_effect=fake_run):
            with self.assertRaises(SystemExit) as context:
                kubernetes_auth_preflight(
                    Path("/kube/config"), "coinjoin-test", reuse_namespace=False
                )

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

        with mock.patch("client.kubernetes.subprocess.run", side_effect=fake_run):
            kubernetes_auth_preflight(Path("/kube/config"), "coinjoin-test", reuse_namespace=False)

    def test_compose_manager_command_sets_default_emulator_image_prefix(self):
        compose_yaml = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn(
            "python manager.py --engine ${COINJOIN_ENGINE:-wasabi} --run-timezone \\\"$${RUN_TIMEZONE}\\\" run "
            "--joinmarket-descriptor-regtest-fallback",
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
        self.assertIn("Skipping joinmarket-client-server pull; local build requested", compose_yaml)
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

    def test_compose_caps_blocksci_to_the_exported_run_tip(self):
        compose_yaml = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("EXPORTED_MAX_BLOCK", compose_yaml)
        self.assertIn('BLOCKSCI_MAX_BLOCK="$$((EXPORTED_MAX_BLOCK + 1))"', compose_yaml)
        self.assertIn('--max-block "$$BLOCKSCI_MAX_BLOCK"', compose_yaml)

    def test_kubernetes_emulation_mounts_scenarios_at_container_scenario_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            host_client_dir = root / "client"
            scenarios_dir = host_client_dir / "scenarios"
            kubeconfig = root / "kubeconfig.yaml"
            scenarios_dir.mkdir(parents=True)
            kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

            env = {
                "HOST_CLIENT_DIR": str(host_client_dir),
                "CONTAINER_RUNTIME": "podman",
                "COINJOIN_EMULATOR_IMAGE": "coinjoin-emulator:test",
                "KUBERNETES_STORAGE_UID": "1234",
                "KUBERNETES_STORAGE_GID": "5678",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("client.wrapper.run_command") as run_mock, \
                mock.patch("client.wrapper.populate_btc_data_volume") as populate_mock, \
                mock.patch("client.wrapper.kubernetes_auth_preflight"):
                run_kubernetes_emulation(
                    scenario="overactive-local.json",
                    namespace="coinjoin-test",
                    kubeconfig=str(kubeconfig),
                    run_timezone_name="UTC",
                )

            docker_cmd = run_mock.call_args.args[0]
            self.assertIn("coinjoin-emulator:test", docker_cmd)
            self.assertIn("--pull=missing", docker_cmd)
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
            populate_mock.assert_called_once_with((root / "btc-data" / "data").resolve())
            self.assertNotIn(f"{scenarios_dir.resolve()}:/app/scenarios:ro", docker_cmd)

    def test_kubernetes_emulation_copy_to_host_preserves_download_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            host_client_dir = root / "client"
            scenarios_dir = host_client_dir / "scenarios"
            kubeconfig = root / "kubeconfig.yaml"
            scenarios_dir.mkdir(parents=True)
            kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

            env = {
                "HOST_CLIENT_DIR": str(host_client_dir),
                "CONTAINER_RUNTIME": "podman",
                "COINJOIN_EMULATOR_IMAGE": "coinjoin-emulator:test",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("client.wrapper.run_command") as run_mock, \
                mock.patch("client.wrapper.populate_btc_data_volume") as populate_mock, \
                mock.patch("client.wrapper.kubernetes_auth_preflight"):
                run_kubernetes_emulation(
                    scenario="overactive-local.json",
                    kubeconfig=str(kubeconfig),
                    copy_to_host=True,
                )

            docker_cmd = run_mock.call_args.args[0]
            self.assertIn("--download-btc-data", docker_cmd)
            self.assertEqual(
                docker_cmd[docker_cmd.index("--download-btc-data") + 1],
                "/btc-data/data",
            )
            self.assertIn(f"{(root / 'btc-data').resolve()}:/btc-data:rw", docker_cmd)
            populate_mock.assert_called_once_with((root / "btc-data" / "data").resolve())

    def test_container_run_pull_args_default_to_always_for_registry_images(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                container_run_pull_args("ghcr.io/ondrejman/coinjoin-emulator:latest", "COINJOIN_EMULATOR_PULL_POLICY"),
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
                container_run_pull_args("ghcr.io/ondrejman/coinjoin-emulator:latest", "COINJOIN_EMULATOR_PULL_POLICY"),
                ["--pull=never"],
            )


if __name__ == "__main__":
    unittest.main()
