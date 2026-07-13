import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from client.pbs import (  # noqa: E402
    PBSError,
    blocksci_export_pbs_command,
    blocksci_pbs_command,
    coinjoin_analysis_pbs_command,
    render_blocksci_pbs,
    render_coinjoin_analysis_pbs,
    render_mappings_pbs,
    require_qsub,
    require_storage_path,
    submit_pbs,
    submit_blocksci_pbs,
    submit_coinjoin_analysis_pbs,
    wait_for_pbs_marker,
)


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
        self.assertIn('status="partial" if', script)
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
            test_values=False,
        )
        self.assertIn("blocksci_parser", command)
        self.assertIn("unified_report.py", command)
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
            test_values=False,
            blocksci_script="/runs/emulation/logs/run-a/.pipeline/blocksci-script.py",
        )

        script_index = command.index(
            "python3 /runs/emulation/logs/run-a/.pipeline/blocksci-script.py"
        )
        report_index = command.index("python3 /mnt/exporters/unified_report.py")
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
            test_values=False,
            include_report=False,
        )

        self.assertIn("blocksci_parser", command)
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
            test_values=True,
        )

        self.assertIn("unified_report.py", command)
        self.assertIn("--test-values", command)
        self.assertNotIn("blocksci_parser", command)

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
        require_storage_path(Path("/storage/brno2/home/xman/run-a"))


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


if __name__ == "__main__":
    unittest.main()
