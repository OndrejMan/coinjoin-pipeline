import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from exporters.artifact_paths import BASELINE_FILE, report_input_hashes  # noqa: E402
from exporters.artifact_paths import RUN_MANIFEST as MANIFEST_NAME  # noqa: E402

from coinjoin_pipeline.execution.run_catalog import (  # noqa: E402
    create_external_manifest,
    discover_runs,
    report_status,
    stage_state,
    write_manifest,
)


class RunCatalogTests(unittest.TestCase):
    def test_discovers_emulator_and_external_runs_with_stages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            emulator = root / "emulator-run"
            (emulator / "coinjoin_emulator_data").mkdir(parents=True)
            (emulator / "coinjoinPipeline_data").mkdir()
            (emulator / "coinjoinPipeline_data" / "unified_report.json").write_text("{}")
            external = root / "external-run"
            external.mkdir()
            write_manifest(external, {"mode": "external", "run_id": "external-run"})

            states = {state.run_dir.name: state for state in discover_runs(root)}

            self.assertEqual(states["emulator-run"].mode, "emulator")
            self.assertEqual(states["emulator-run"].stages["emulation"], "present")
            self.assertEqual(states["emulator-run"].stages["report"], "present")
            self.assertEqual(states["emulator-run"].report_status, "diagnostics_missing")
            self.assertEqual(report_status(emulator), "diagnostics_missing")
            self.assertEqual(states["external-run"].mode, "external")

    def test_report_status_surfaces_failed_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            report_dir = run_dir / "coinjoinPipeline_data"
            report_dir.mkdir()
            (report_dir / "unified_report.json").write_text(
                json.dumps({"integration_diagnostics": {"status": "not_ok"}})
            )
            self.assertEqual(report_status(run_dir), "diagnostics_not_ok")

    def test_report_status_surfaces_unavailable_emulator_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            report_dir = run_dir / "coinjoinPipeline_data"
            report_dir.mkdir()
            (report_dir / "unified_report.json").write_text(
                json.dumps(
                    {
                        "evaluation_scope": "emulator_labels_unavailable",
                        "integration_diagnostics": {"status": "ok"},
                    }
                )
            )
            self.assertEqual(report_status(run_dir), "emulator_labels_unavailable")

    def test_report_status_fails_closed_on_unknown_diagnostics_status(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            report_dir = run_dir / "coinjoinPipeline_data"
            report_dir.mkdir()
            (report_dir / "unified_report.json").write_text(
                json.dumps({"integration_diagnostics": {"status": "unavailable"}})
            )
            self.assertEqual(report_status(run_dir), "diagnostics_not_ok")

    def test_report_status_flags_report_older_than_upstream_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            report_dir = run_dir / "coinjoinPipeline_data"
            report_dir.mkdir()
            report = report_dir / "unified_report.json"
            report.write_text(json.dumps({"integration_diagnostics": {"status": "ok"}}))
            import os

            os.utime(report, (1000, 1000))
            baseline = run_dir / BASELINE_FILE
            baseline.parent.mkdir(parents=True)
            baseline.write_text("{}")
            os.utime(baseline, (2000, 2000))

            self.assertEqual(report_status(run_dir), "stale")
            self.assertEqual(stage_state(run_dir)["report"], "stale")

    def test_external_manifest_fingerprints_baseline_without_copying_datadir(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            datadir = root / "bitcoin"
            (datadir / "blocks").mkdir(parents=True)
            baseline = root / "baseline.json"
            baseline.write_text(json.dumps({"coinjoins": {}}))

            manifest = create_external_manifest(run_dir, datadir, baseline, "bitcoin", "wasabi2")
            write_manifest(run_dir, manifest)

            saved = json.loads((run_dir / MANIFEST_NAME).read_text())
            self.assertEqual(saved["mode"], "external")
            self.assertEqual(saved["inputs"]["bitcoin_datadir"], str(datadir.resolve()))
            self.assertNotIn("blocks", {item.name for item in run_dir.iterdir()})

    def test_stage_state_requires_run_local_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            self.assertEqual(stage_state(run_dir)["baseline"], "missing")
            target = run_dir / BASELINE_FILE
            target.parent.mkdir(parents=True)
            target.write_text("{}")
            self.assertEqual(stage_state(run_dir)["baseline"], "present")

    def test_write_manifest_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_manifest(run_dir, {"mode": "external"})
            with self.assertRaises(FileExistsError):
                write_manifest(run_dir, {"mode": "external"})

    def _report_with_recorded_inputs(self, run_dir):
        baseline = run_dir / BASELINE_FILE
        baseline.parent.mkdir(parents=True)
        baseline.write_text("{}")
        report_dir = run_dir / "coinjoinPipeline_data"
        report_dir.mkdir()
        report = {
            "integration_diagnostics": {"status": "ok"},
            "run_manifest": {"inputs": report_input_hashes(run_dir)},
        }
        (report_dir / "unified_report.json").write_text(json.dumps(report))
        return baseline

    def test_recorded_input_hashes_ignore_timestamps(self):
        import os

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            baseline = self._report_with_recorded_inputs(run_dir)
            # A copy or download refreshes timestamps without changing content.
            os.utime(baseline, (4_000_000_000, 4_000_000_000))
            self.assertEqual(report_status(run_dir), "complete")

    def test_changed_or_new_input_makes_a_hashed_report_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            baseline = self._report_with_recorded_inputs(run_dir)
            baseline.write_text('{"changed": true}')
            self.assertEqual(report_status(run_dir), "stale")
            self.assertEqual(stage_state(run_dir)["report"], "stale")

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            self._report_with_recorded_inputs(run_dir)
            mappings = run_dir / "coinjoin-mappings_data" / "coinjoin_mappings.json"
            mappings.parent.mkdir()
            mappings.write_text("{}")
            self.assertEqual(report_status(run_dir), "stale")

    def test_inputs_missing_locally_do_not_make_a_report_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            baseline = self._report_with_recorded_inputs(run_dir)
            baseline.unlink()
            self.assertEqual(report_status(run_dir), "complete")
