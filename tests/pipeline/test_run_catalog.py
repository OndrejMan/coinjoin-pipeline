import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from exporters.artifact_paths import BASELINE_FILE  # noqa: E402
from exporters.manifest import report_input_hashes  # noqa: E402

from coinjoin_pipeline.execution.run_catalog import (  # noqa: E402
    report_status,
    stage_state,
)


def write_report(run_dir: Path, **fields: object) -> Path:
    """A report that recorded the hashes of the inputs present now."""
    report = run_dir / "coinjoinPipeline_data" / "unified_report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"run_manifest": {"inputs": report_input_hashes(run_dir)}, **fields}))
    return report


class RunCatalogTests(unittest.TestCase):
    def test_report_status_surfaces_failed_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_report(run_dir, integration_diagnostics={"status": "not_ok"})
            self.assertEqual(report_status(run_dir), "diagnostics_not_ok")

    def test_report_status_surfaces_unavailable_emulator_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_report(
                run_dir,
                evaluation_scope="emulator_labels_unavailable",
                integration_diagnostics={"status": "ok"},
            )
            self.assertEqual(report_status(run_dir), "emulator_labels_unavailable")

    def test_report_status_fails_closed_on_unknown_diagnostics_status(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_report(run_dir, integration_diagnostics={"status": "unavailable"})
            self.assertEqual(report_status(run_dir), "diagnostics_not_ok")

    def test_report_status_flags_report_without_recorded_inputs_as_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            report_dir = run_dir / "coinjoinPipeline_data"
            report_dir.mkdir()
            (report_dir / "unified_report.json").write_text(json.dumps({"integration_diagnostics": {"status": "ok"}}))
            baseline = run_dir / BASELINE_FILE
            baseline.parent.mkdir(parents=True)
            baseline.write_text("{}")

            self.assertEqual(report_status(run_dir), "stale")
            self.assertEqual(stage_state(run_dir)["report"], "stale")

    def test_stage_state_requires_run_local_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            self.assertEqual(stage_state(run_dir)["baseline"], "missing")
            target = run_dir / BASELINE_FILE
            target.parent.mkdir(parents=True)
            target.write_text("{}")
            self.assertEqual(stage_state(run_dir)["baseline"], "present")

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
