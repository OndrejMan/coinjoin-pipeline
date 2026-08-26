import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from client.artifacts import (  # noqa: E402
    PROBE_QUEUED,
    PROBE_RUNNING,
    PROBE_TERMINAL,
    PROBE_UNKNOWN,
)
from client.pbs import (  # noqa: E402
    PBS_ACTIVE_STATES,
    PBS_QUEUED_STATES,
    PBS_TERMINAL_STATES,
    PBSError,
    _qstat_job_state,
    persist_pbs_job_id,
    qdel_pbs_job,
    blocksci_export_pbs_command,
    blocksci_pbs_command,
    blocksci_script_pbs_command,
    coinjoin_analysis_pbs_command,
    pbs_job_probe,
    render_blocksci_pbs,
    render_coinjoin_analysis_pbs,
    render_mappings_pbs,
    require_qsub,
    require_storage_path,
    submit_pbs,
    submit_pbs_text,
    submit_blocksci_pbs,
    report_stage_log,
    stage_log_path,
    submit_coinjoin_analysis_pbs,
    wait_for_pbs_marker,
)


class PBSJobProbeTest(unittest.TestCase):
    def _probe_state(self, qstat_state):
        with mock.patch("client.pbs._qstat_job_state", return_value=qstat_state):
            return pbs_job_probe("7.server")()

    def test_terminal_states_map_to_terminal(self):
        # "X" is terminal for the watcher too; the two must not disagree.
        for state in ("C", "F", "X", "MISSING"):
            self.assertEqual(self._probe_state(state), PROBE_TERMINAL)

    def test_active_states_map_to_running(self):
        for state in ("B", "E", "M", "R", "S", "T", "U"):
            self.assertEqual(self._probe_state(state), PROBE_RUNNING)

    def test_queued_states_map_to_queued(self):
        for state in ("Q", "H", "W"):
            self.assertEqual(self._probe_state(state), PROBE_QUEUED)

    def test_every_recognized_state_has_a_probe_verdict(self):
        """No recognized qstat state may fall through to the raise."""
        for state in PBS_TERMINAL_STATES | PBS_ACTIVE_STATES:
            with self.subTest(state=state):
                self.assertIn(
                    self._probe_state(state),
                    (PROBE_TERMINAL, PROBE_RUNNING, PROBE_QUEUED),
                )

    def test_terminal_and_active_state_sets_are_disjoint(self):
        self.assertEqual(PBS_TERMINAL_STATES & PBS_ACTIVE_STATES, set())
        self.assertLessEqual(PBS_QUEUED_STATES, PBS_ACTIVE_STATES)

    def test_inconclusive_qstat_maps_to_unknown(self):
        self.assertEqual(self._probe_state(None), PROBE_UNKNOWN)

    def test_unexpected_state_raises(self):
        with self.assertRaises(PBSError):
            self._probe_state("Z")

    def test_qstat_falls_back_when_job_history_is_disabled(self):
        history_disabled = subprocess.CompletedProcess(
            ["qstat", "-x", "-f", "7.server"],
            1,
            stdout="",
            stderr="qstat: PBS is not configured to maintain job history",
        )
        missing = subprocess.CompletedProcess(
            ["qstat", "-f", "7.server"],
            153,
            stdout="",
            stderr="qstat: Unknown Job Id 7.server",
        )
        with (
            mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qstat"),
            mock.patch("client.pbs.subprocess.run", side_effect=[history_disabled, missing]) as run,
        ):
            self.assertEqual(_qstat_job_state("7.server"), "MISSING")
        self.assertEqual(run.call_args_list[1].args[0], ["qstat", "-f", "7.server"])


class PBSStateSetParityTest(unittest.TestCase):
    """client.pbs owns the state sets; the watcher copies them.

    src/coinjoin_pipeline/watch.py cannot import client.pbs (pipeline/ is a
    subprocess runtime root, not a packaged module), so the duplicate is
    asserted here instead of being allowed to drift.
    """

    def test_watch_terminal_states_match_client_pbs(self):
        watch_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "coinjoin_pipeline"
            / "watch.py"
        )
        namespace: dict[str, object] = {}
        for line in watch_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("PBS_TERMINAL_STATES"):
                exec(line, namespace)  # noqa: S102 - constant literal only
                break
        self.assertEqual(namespace["PBS_TERMINAL_STATES"], PBS_TERMINAL_STATES)


class PBSQdelTest(unittest.TestCase):
    def test_qdel_reports_success(self):
        with (
            mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qdel"),
            mock.patch("client.pbs.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertTrue(qdel_pbs_job("7.server"))

    def test_qdel_reports_failure_when_unavailable(self):
        """Rollback must not claim success when qdel is not installed."""
        with mock.patch("client.pbs.shutil.which", return_value=None):
            self.assertFalse(qdel_pbs_job("7.server"))

    def test_qdel_reports_failure_when_scheduler_rejects(self):
        with (
            mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qdel"),
            mock.patch("client.pbs.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="qdel: Permission denied"
            )
            self.assertFalse(qdel_pbs_job("7.server"))

    def test_qdel_treats_already_finished_job_as_cancelled(self):
        with (
            mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qdel"),
            mock.patch("client.pbs.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="qdel: Unknown Job Id 7.server"
            )
            self.assertTrue(qdel_pbs_job("7.server"))


class PBSJobIdPersistenceTest(unittest.TestCase):
    def test_persist_writes_job_id_atomically(self):
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            persist_pbs_job_id(run_dir, "blocksci", "7.server")
            jobid_path = run_dir / ".pbs" / "blocksci.jobid"
            self.assertEqual(jobid_path.read_text(encoding="utf-8"), "7.server\n")
            # No temp file may survive a successful write.
            self.assertEqual(
                sorted(p.name for p in (run_dir / ".pbs").iterdir()),
                ["blocksci.jobid"],
            )

    def test_persist_leaves_no_partial_file_when_write_fails(self):
        """An interrupted write must not leave an empty .jobid file.

        ensure_no_active_s3_pbs_submission() skips empty files, so a truncated
        record would let a duplicate graph be submitted for a live run.
        """
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            persist_pbs_job_id(run_dir, "blocksci", "7.server")
            with mock.patch("client.pbs.os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    persist_pbs_job_id(run_dir, "blocksci", "8.server")
            marker_dir = run_dir / ".pbs"
            # Previous good value intact, no stray temp file left behind.
            self.assertEqual(
                (marker_dir / "blocksci.jobid").read_text(encoding="utf-8"),
                "7.server\n",
            )
            self.assertEqual(
                sorted(p.name for p in marker_dir.iterdir()), ["blocksci.jobid"]
            )


class PBSStdinSubmissionTest(unittest.TestCase):
    def test_submit_pbs_text_pipes_script_and_dependency(self):
        with mock.patch("client.pbs.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="9.server\n", stderr="")
            job_id = submit_pbs_text("#PBS -N stage\ntrue\n", "8.server")
        self.assertEqual(job_id, "9.server")
        self.assertEqual(run.call_args.args[0], ["qsub", "-W", "depend=afterok:8.server"])
        self.assertEqual(run.call_args.kwargs["input"], "#PBS -N stage\ntrue\n")

    def test_submit_pbs_text_supports_multiple_dependencies(self):
        with mock.patch("client.pbs.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="10.server\n", stderr="")
            job_id = submit_pbs_text(
                "#PBS -N report\ntrue\n",
                ("8.server", "9.server"),
            )
        self.assertEqual(job_id, "10.server")
        self.assertEqual(
            run.call_args.args[0],
            ["qsub", "-W", "depend=afterok:8.server:9.server"],
        )

    def test_submit_pbs_text_raises_on_qsub_failure(self):
        with mock.patch("client.pbs.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="bad queue")
            with self.assertRaises(PBSError):
                submit_pbs_text("#PBS -N stage\ntrue\n")

    def test_submit_pbs_text_rejects_empty_job_id_on_zero_exit(self):
        """A zero exit with no job ID may still have queued a job.

        Returning "" makes record_job() skip persistence, so the job would be
        untracked: no .jobid file, no dependency target, and rollback could not
        cancel it. Fail loudly instead.
        """
        for stdout in ("", "   \n"):
            with self.subTest(stdout=stdout):
                with mock.patch("client.pbs.subprocess.run") as run:
                    run.return_value = mock.Mock(returncode=0, stdout=stdout, stderr="")
                    with self.assertRaisesRegex(PBSError, "invalid job ID"):
                        submit_pbs_text("#PBS -N stage\ntrue\n")

    def test_submit_pbs_text_rejects_multiline_job_id(self):
        with mock.patch("client.pbs.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="warning: queue is full\n11.server\n",
                stderr="",
            )
            with self.assertRaisesRegex(PBSError, "invalid job ID"):
                submit_pbs_text("#PBS -N stage\ntrue\n")

    def test_submit_pbs_text_rejects_unsafe_job_id(self):
        with mock.patch("client.pbs.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0, stdout="11.server; rm -rf /\n", stderr=""
            )
            with self.assertRaises(PBSError):
                submit_pbs_text("#PBS -N stage\ntrue\n")

    def test_submit_pbs_text_accepts_metacentrum_job_id(self):
        with mock.patch("client.pbs.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="12345678.meta-pbs.metacentrum.cz\n",
                stderr="",
            )
            self.assertEqual(
                submit_pbs_text("#PBS -N stage\ntrue\n"),
                "12345678.meta-pbs.metacentrum.cz",
            )

    def test_submit_pbs_rejects_empty_job_id_on_zero_exit(self):
        with mock.patch("client.pbs.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="\n", stderr="")
            with self.assertRaisesRegex(PBSError, "invalid job ID"):
                submit_pbs(Path("/storage/run-a/job.pbs"))


class PBSTemplateTest(unittest.TestCase):
    def test_render_mappings_pbs_runs_both_images_and_writes_markers(self):
        script = render_mappings_pbs(
            Path("/storage/run-a"),
            "docker://enumerator",
            "docker://sake",
            timeout=60,
            retry_timeout=600,
            sake_seed=42,
        )
        self.assertIn('case "docker://enumerator" in', script)
        self.assertIn('case "docker://sake" in', script)
        self.assertIn(
            'singularity pull --force "$ENUMERATOR_SIF" "docker://enumerator"', script
        )
        self.assertIn('cp "docker://enumerator" "$ENUMERATOR_SIF"', script)
        self.assertIn('"$ENUMERATOR_SIF" python3 /app/run.py', script)
        self.assertIn('"$SAKE_SIF" dotnet /app/Sake.dll', script)
        self.assertIn("--timeout 60 --retry-timeout 600", script)
        self.assertIn("--seed 42", script)
        self.assertIn('SINGULARITY_CACHEDIR="$SCRATCHDIR"', script)
        self.assertIn('ENUMERATOR_DIGEST="sha256:', script)
        self.assertIn("ENUMERATOR_STATUS=$?", script)
        self.assertIn("SAKE_STATUS=$?", script)
        self.assertIn('get("errors",0)>0', script)
        self.assertIn('status="partial" if', script)
        self.assertIn('ss.get("errors",0)', script)
        self.assertIn("coinjoin_mappings.json", script)
        self.assertIn("coinjoin-mappings.done", script)
        self.assertIn("coinjoin-mappings.failed", script)

    def test_render_blocksci_pbs_includes_select_line(self):
        run_dir = Path("/storage/run-a")
        script = render_blocksci_pbs(
            run_dir,
            Path("/storage/logs"),
            Path("/storage/bitcoin-data"),
            Path("/storage/exporters"),
            "docker://image",
            "echo hello",
            ncpus=8,
            mem="64gb",
            scratch="100gb",
            walltime="24:00:00",
        )
        self.assertIn("#PBS -l select=1:ncpus=8:mem=64gb:scratch_local=100gb", script)
        self.assertIn("#PBS -l walltime=24:00:00", script)
        self.assertIn("/storage/run-a", script)
        self.assertIn('BITCOIN_DATADIR="/storage/bitcoin-data"', script)
        self.assertIn('--bind "$BITCOIN_DATADIR:/mnt/data:ro"', script)
        self.assertIn('--bind "$EXPORTERS_DIR:/mnt/exporters:ro"', script)
        self.assertIn("echo hello", script)
        self.assertIn("singularity exec", script)
        self.assertNotIn("docker run", script)
        self.assertNotIn("EXECUTOR", script)
        self.assertNotIn("PBS_JOB_CONTAINER_RUNTIME", script)

    def test_render_coinjoin_analysis_pbs_includes_select_line(self):
        run_dir = Path("/storage/run-a")
        script = render_coinjoin_analysis_pbs(
            run_dir,
            run_dir / "coinjoin-analysis_data",
            run_dir / "coinjoin_emulator_data" / "data",
            "docker://image",
            "analyze-emul",
            ncpus=4,
            mem="16gb",
            scratch="50gb",
            walltime="04:00:00",
        )
        self.assertIn("#PBS -l select=1:ncpus=4:mem=16gb:scratch_local=50gb", script)
        self.assertIn("analyze-emul", script)
        self.assertIn('OUTPUT_DIR="/storage/run-a/coinjoin-analysis_data"', script)
        self.assertIn(
            'INPUT_DATA_DIR="/storage/run-a/coinjoin_emulator_data/data"', script
        )
        self.assertIn(
            '--bind "$OUTPUT_DIR:/runs/emulation/selected/$(basename "$RUN_DIR"):rw"',
            script,
        )
        self.assertIn(
            '--bind "$INPUT_DATA_DIR:/runs/emulation/selected/$(basename "$RUN_DIR")/data:ro"',
            script,
        )
        self.assertIn("singularity exec", script)
        self.assertNotIn("docker run", script)
        self.assertNotIn("EXECUTOR", script)
        self.assertNotIn("PBS_JOB_CONTAINER_RUNTIME", script)

    def test_blocksci_pbs_command_contains_parser_and_report(self):
        run_dir = Path("/storage/run-a")
        command = blocksci_pbs_command(
            run_id=run_dir.name,
            coinjoin_type="joinmarket",
            min_input_count=1,
            joinmarket_detector="definite",
            joinmarket_min_base_fee=5000,
            joinmarket_percentage_fee=0.00004,
            joinmarket_max_depth=200000,
        )
        self.assertIn("blocksci_parser", command)
        self.assertIn("unified_report.py", command)
        self.assertIn(
            "PYTHONPATH=/blocksci/.venv/lib/python3.8/site-packages:"
            "/mnt/blocksci/blockscipy /usr/bin/python3",
            command,
        )
        self.assertIn("--disk /mnt/data/regtest", command)
        self.assertIn("--coinjoin-type joinmarket", command)

    def test_blocksci_pbs_command_runs_custom_script_before_report(self):
        command = blocksci_pbs_command(
            run_id="run-a",
            coinjoin_type="joinmarket",
            min_input_count=1,
            joinmarket_detector="definite",
            joinmarket_min_base_fee=5000,
            joinmarket_percentage_fee=0.00004,
            joinmarket_max_depth=200000,
            blocksci_script="/runs/emulation/logs/run-a/.pipeline/blocksci-script.py",
        )

        script_index = command.index(
            "PYTHONPATH=/blocksci/.venv/lib/python3.8/site-packages:"
            "/mnt/blocksci/blockscipy /usr/bin/python3 "
            "/runs/emulation/logs/run-a/.pipeline/blocksci-script.py"
        )
        report_index = command.index(
            "/usr/bin/python3 /mnt/exporters/unified_report.py"
        )
        self.assertLess(script_index, report_index)
        self.assertIn(
            "BLOCKSCI_CONFIG=/runs/emulation/logs/run-a/blocksci_data/config.json",
            command,
        )

    def test_blocksci_pbs_command_can_defer_report(self):
        command = blocksci_pbs_command(
            run_id="run-a",
            coinjoin_type="joinmarket",
            min_input_count=1,
            joinmarket_detector="definite",
            joinmarket_min_base_fee=5000,
            joinmarket_percentage_fee=0.00004,
            joinmarket_max_depth=200000,
            include_report=False,
        )

        self.assertIn("blocksci_parser", command)
        self.assertNotIn("unified_report.py", command)

    def test_blocksci_pbs_command_can_persist_analysis_for_lightweight_report(self):
        command = blocksci_pbs_command(
            run_id="run-a",
            coinjoin_type="wasabi2",
            min_input_count=None,
            joinmarket_detector="definite",
            joinmarket_min_base_fee=5000,
            joinmarket_percentage_fee=0.00004,
            joinmarket_max_depth=200000,
            include_report=False,
            export_analysis=True,
        )

        self.assertIn("blocksci_parser", command)
        self.assertIn(
            "/usr/bin/python3 /mnt/exporters/blocksci_export/analysis.py",
            command,
        )
        self.assertIn("--min-input-count default", command)
        self.assertNotIn("unified_report.py", command)

    def test_blocksci_export_pbs_command_is_report_only(self):
        command = blocksci_export_pbs_command(
            run_id="run-a",
            coinjoin_type="joinmarket",
            min_input_count=1,
            joinmarket_detector="definite",
            joinmarket_min_base_fee=5000,
            joinmarket_percentage_fee=0.00004,
            joinmarket_max_depth=200000,
        )

        self.assertIn("unified_report.py", command)
        self.assertIn("--blocksci-analysis", command)
        self.assertNotIn("blocksci_parser", command)

    def test_blocksci_script_command_exports_typed_detector_settings(self):
        command = blocksci_script_pbs_command(
            run_id="mainnet-850000",
            coinjoin_type="joinmarket",
            min_input_count=None,
            joinmarket_detector="possible",
            joinmarket_min_base_fee=4000,
            joinmarket_percentage_fee=0.00005,
            joinmarket_max_depth=150000,
        )

        self.assertIn("BLOCKSCI_CONFIG=", command)
        self.assertIn("BLOCKSCI_OUTPUT_DIR=", command)
        self.assertIn("COINJOIN_TYPE=joinmarket", command)
        self.assertIn("JOINMARKET_DETECTOR=possible", command)
        self.assertIn("JOINMARKET_MIN_BASE_FEE=4000", command)
        self.assertIn("JOINMARKET_PERCENTAGE_FEE=5e-05", command)
        self.assertIn("JOINMARKET_MAX_DEPTH=150000", command)
        self.assertNotIn("MIN_INPUT_COUNT=", command)
        self.assertTrue(
            command.endswith("/usr/bin/python3 /mnt/user-analysis.py")
        )

    def test_coinjoin_analysis_pbs_command_supports_analyze_only(self):
        command = coinjoin_analysis_pbs_command("analyze_only")

        self.assertIn("--action analyze_only", command)


class PBSValidationTest(unittest.TestCase):
    def test_render_rejects_resource_directive_injection(self):
        with self.assertRaisesRegex(PBSError, "memory"):
            render_blocksci_pbs(
                Path("/storage/run-a"),
                Path("/storage/logs"),
                Path("/storage/bitcoin-data"),
                Path("/storage/exporters"),
                "docker://image",
                "echo hello",
                mem="64gb\n#PBS -q attacker",
            )

    def test_render_rejects_image_shell_injection(self):
        with self.assertRaisesRegex(PBSError, "container image"):
            render_coinjoin_analysis_pbs(
                Path("/storage/run-a"),
                Path("/storage/run-a/output"),
                Path("/storage/run-a/input"),
                'docker://image"; touch /tmp/injected; #',
                "analyze-emul",
            )

    def test_render_rejects_invalid_walltime_components(self):
        with self.assertRaisesRegex(PBSError, "walltime"):
            render_mappings_pbs(
                Path("/storage/run-a"),
                "docker://enumerator",
                "docker://sake",
                walltime="04:99:00",
            )

    def test_require_qsub_raises_when_missing(self):
        with mock.patch("client.pbs.shutil.which", return_value=None):
            with self.assertRaises(PBSError):
                require_qsub()

    def test_require_qsub_passes_when_available(self):
        with mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qsub"):
            require_qsub()

    def test_require_storage_path_rejects_non_storage_path(self):
        with self.assertRaises(PBSError):
            require_storage_path(Path("/tmp/run-a"))

    def test_require_storage_path_accepts_storage_path(self):
        require_storage_path(Path("/storage/brno2/home/user/run-a"))


class PBSSubmissionTest(unittest.TestCase):
    def test_submit_pbs_supports_afterok_dependency(self):
        with mock.patch("client.pbs.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                [], 0, stdout="block-job.meta\n", stderr=""
            )
            job_id = submit_pbs(Path("/tmp/blocksci.pbs"), "analysis-job.meta")
        self.assertEqual(job_id, "block-job.meta")
        self.assertEqual(
            run_mock.call_args.args[0],
            [
                "qsub",
                "-W",
                "depend=afterok:analysis-job.meta",
                "/tmp/blocksci.pbs",
            ],
        )

    def test_submit_pbs_supports_multiple_afterok_dependencies(self):
        with mock.patch("client.pbs.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                [], 0, stdout="report-job.meta\n", stderr=""
            )
            job_id = submit_pbs(
                Path("/tmp/report.pbs"),
                ("analysis-job.meta", "blocksci-job.meta"),
            )
        self.assertEqual(job_id, "report-job.meta")
        self.assertEqual(
            run_mock.call_args.args[0],
            [
                "qsub",
                "-W",
                "depend=afterok:analysis-job.meta:blocksci-job.meta",
                "/tmp/report.pbs",
            ],
        )

    def test_submit_blocksci_pbs_writes_script_and_calls_qsub(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            with (
                mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qsub"),
                mock.patch("client.pbs.require_storage_path"),
                mock.patch("client.pbs.require_existing_path"),
                mock.patch("client.pbs.require_bitcoin_datadir"),
                mock.patch("client.pbs.subprocess.run") as run_mock,
            ):
                run_mock.return_value = subprocess.CompletedProcess(
                    [],
                    0,
                    stdout="12345.meta-pbs\n",
                    stderr="",
                )
                job_id = submit_blocksci_pbs(
                    run_dir=run_dir,
                    logs_root=run_dir.parent,
                    bitcoin_datadir=run_dir / "bitcoin-data",
                    exporters_dir=run_dir / "exporters",
                    image="docker://image",
                    command="echo hello",
                    ncpus=8,
                    mem="64gb",
                    scratch="100gb",
                    walltime="24:00:00",
                    dry_run=False,
                )
            self.assertEqual(job_id, "12345.meta-pbs")
            pbs_script = run_dir / ".pbs" / "blocksci.pbs"
            self.assertTrue(pbs_script.exists())
            qsub_call = run_mock.call_args
            self.assertEqual(qsub_call.args[0], ["qsub", str(pbs_script)])

    def test_submit_blocksci_pbs_supports_report_stage_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            with (
                mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qsub"),
                mock.patch("client.pbs.require_storage_path"),
                mock.patch("client.pbs.require_existing_path"),
                mock.patch("client.pbs.require_bitcoin_datadir"),
                mock.patch("client.pbs.subprocess.run") as run_mock,
            ):
                run_mock.return_value = subprocess.CompletedProcess(
                    [], 0, stdout="42\n", stderr=""
                )
                submit_blocksci_pbs(
                    run_dir=run_dir,
                    logs_root=run_dir.parent,
                    bitcoin_datadir=run_dir / "bitcoin-data",
                    exporters_dir=run_dir / "exporters",
                    image="docker://image",
                    command="echo report",
                    stage="unified-report",
                    job_name="blocksci_unified_report",
                )

            script_path = run_dir / ".pbs" / "unified-report.pbs"
            script = script_path.read_text(encoding="utf-8")
            self.assertIn("#PBS -N blocksci_unified_report", script)
            self.assertIn("unified-report.done", script)
            self.assertIn("unified-report.failed", script)

    def test_submit_coinjoin_analysis_pbs_dry_run_does_not_submit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            with (
                mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qsub"),
                mock.patch("client.pbs.require_storage_path"),
                mock.patch("client.pbs.subprocess.run") as run_mock,
            ):
                input_data_dir = run_dir / "coinjoin_emulator_data" / "data"
                input_data_dir.mkdir(parents=True)
                job_id = submit_coinjoin_analysis_pbs(
                    run_dir=run_dir,
                    output_dir=run_dir / "coinjoin-analysis_data",
                    input_data_dir=input_data_dir,
                    image="docker://image",
                    command="analyze-emul",
                    ncpus=4,
                    mem="16gb",
                    scratch="50gb",
                    walltime="04:00:00",
                    dry_run=True,
                )
            self.assertIsNone(job_id)
            run_mock.assert_not_called()

    def test_submit_blocksci_pbs_dry_run_prints_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            with (
                mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qsub"),
                mock.patch("client.pbs.require_storage_path"),
                mock.patch("client.pbs.require_existing_path"),
                mock.patch("client.pbs.require_bitcoin_datadir"),
            ):
                job_id = submit_blocksci_pbs(
                    run_dir=run_dir,
                    logs_root=run_dir.parent,
                    bitcoin_datadir=run_dir / "bitcoin-data",
                    exporters_dir=run_dir / "exporters",
                    image="docker://image",
                    command="echo hello",
                    ncpus=8,
                    mem="64gb",
                    scratch="100gb",
                    walltime="24:00:00",
                    dry_run=True,
                )
            self.assertIsNone(job_id)
            pbs_script = run_dir / ".pbs" / "blocksci.pbs"
            self.assertTrue(pbs_script.exists())

    def test_submit_blocksci_pbs_raises_on_qsub_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            with (
                mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qsub"),
                mock.patch("client.pbs.require_storage_path"),
                mock.patch("client.pbs.require_existing_path"),
                mock.patch("client.pbs.require_bitcoin_datadir"),
                mock.patch("client.pbs.subprocess.run") as run_mock,
            ):
                run_mock.return_value = subprocess.CompletedProcess(
                    [],
                    1,
                    stdout="",
                    stderr="qsub: error\n",
                )
                with self.assertRaises(PBSError):
                    submit_blocksci_pbs(
                        run_dir=run_dir,
                        logs_root=run_dir.parent,
                        bitcoin_datadir=run_dir / "bitcoin-data",
                        exporters_dir=run_dir / "exporters",
                        image="docker://image",
                        command="echo hello",
                        ncpus=8,
                        mem="64gb",
                        scratch="100gb",
                        walltime="24:00:00",
                        dry_run=False,
                    )

    def test_submit_blocksci_pbs_rejects_non_storage_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            with mock.patch("client.pbs.shutil.which", return_value="/usr/bin/qsub"):
                with self.assertRaises(PBSError):
                    submit_blocksci_pbs(
                        run_dir=run_dir,
                        logs_root=run_dir.parent,
                        bitcoin_datadir=run_dir / "bitcoin-data",
                        exporters_dir=run_dir / "exporters",
                        image="docker://image",
                        command="echo hello",
                        ncpus=8,
                        mem="64gb",
                        scratch="100gb",
                        walltime="24:00:00",
                        dry_run=False,
                    )


class PBSMarkerWaitTest(unittest.TestCase):
    def test_wait_for_pbs_marker_returns_on_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            done = run_dir / ".pbs" / "blocksci.done"
            done.parent.mkdir(parents=True)
            done.write_text("", encoding="utf-8")
            wait_for_pbs_marker(run_dir, "blocksci", poll_interval=0)

    def test_wait_for_pbs_marker_raises_on_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            failed = run_dir / ".pbs" / "blocksci.failed"
            failed.parent.mkdir(parents=True)
            failed.write_text("", encoding="utf-8")
            with self.assertRaises(PBSError):
                wait_for_pbs_marker(run_dir, "blocksci", poll_interval=0)

    def test_wait_for_pbs_marker_polls_until_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            done = run_dir / ".pbs" / "blocksci.done"
            done.parent.mkdir(parents=True)
            call_count = [0]

            def fake_sleep(_seconds):
                call_count[0] += 1
                if call_count[0] == 2:
                    done.write_text("", encoding="utf-8")

            with mock.patch("client.pbs.time.sleep", side_effect=fake_sleep):
                wait_for_pbs_marker(run_dir, "blocksci", poll_interval=0)
            self.assertGreaterEqual(call_count[0], 2)

    def test_wait_for_pbs_marker_extends_deadline_while_queued(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            done = run_dir / ".pbs" / "blocksci.done"
            done.parent.mkdir(parents=True)
            call_count = [0]

            def fake_sleep(_seconds):
                # A job stuck in the queue (state Q) past the deadline must not
                # be failed; the marker only appears after several extensions.
                call_count[0] += 1
                if call_count[0] == 3:
                    done.write_text("", encoding="utf-8")

            with (
                mock.patch("client.pbs._qstat_job_state", return_value="Q"),
                mock.patch("client.pbs.time.sleep", side_effect=fake_sleep),
            ):
                wait_for_pbs_marker(
                    run_dir, "blocksci", poll_interval=0,
                    job_id="7.server", timeout_seconds=0,
                )
            self.assertGreaterEqual(call_count[0], 3)


class PBSStageLogReportTest(unittest.TestCase):
    """The job log is the only record of *why* a PBS stage failed."""

    def test_report_stage_log_prints_tail_and_returns_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            log_path = stage_log_path(run_dir, "blocksci")
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "\n".join(f"line-{index}" for index in range(10)) + "\n",
                encoding="utf-8",
            )
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                reported = report_stage_log(run_dir, "blocksci", tail_lines=3)
            output = stderr.getvalue()
            self.assertEqual(reported, log_path)
            self.assertIn(str(log_path), output)
            self.assertIn("line-9", output)
            self.assertNotIn("line-6", output)
            self.assertIn("7 earlier lines omitted", output)

    def test_report_stage_log_without_log_does_not_wait(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            run_dir.mkdir()
            with (
                mock.patch("client.pbs.time.sleep") as sleep,
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                reported = report_stage_log(run_dir, "blocksci")
            # Nothing was ever written, so there is no copy-back to wait for.
            sleep.assert_not_called()
            self.assertIsNone(reported)
            self.assertIn("No blocksci job log available", stderr.getvalue())

    def test_report_stage_log_waits_for_copy_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            log_path = stage_log_path(run_dir, "blocksci")
            log_path.parent.mkdir(parents=True)

            def fake_sleep(_seconds):
                log_path.write_text("late arrival\n", encoding="utf-8")

            with (
                mock.patch("client.pbs.time.sleep", side_effect=fake_sleep),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                reported = report_stage_log(run_dir, "blocksci")
            self.assertEqual(reported, log_path)
            self.assertIn("late arrival", stderr.getvalue())

    def test_wait_for_pbs_marker_reports_log_on_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            log_path = stage_log_path(run_dir, "blocksci")
            log_path.parent.mkdir(parents=True)
            log_path.write_text("blocksci_parser: fatal error\n", encoding="utf-8")
            failed = run_dir / ".pbs" / "blocksci.failed"
            failed.parent.mkdir(parents=True)
            failed.write_text("failed", encoding="utf-8")
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(PBSError) as raised:
                    wait_for_pbs_marker(run_dir, "blocksci", poll_interval=0)
            self.assertIn("blocksci_parser: fatal error", stderr.getvalue())
            self.assertIn(str(log_path), str(raised.exception))

    def test_wait_for_pbs_marker_reports_log_when_marker_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run-a"
            log_path = stage_log_path(run_dir, "blocksci")
            log_path.parent.mkdir(parents=True)
            log_path.write_text("killed by the scheduler\n", encoding="utf-8")
            with (
                mock.patch("client.pbs._qstat_job_state", return_value="F"),
                mock.patch("client.pbs.time.sleep"),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                with self.assertRaises(PBSError) as raised:
                    wait_for_pbs_marker(
                        run_dir, "blocksci", poll_interval=0, job_id="7.server"
                    )
            self.assertIn("killed by the scheduler", stderr.getvalue())
            self.assertIn("ended without marker", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
