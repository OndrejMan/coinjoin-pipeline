import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from client.run_catalog import (  # noqa: E402
    BASELINE_FILE,
    MANIFEST_NAME,
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
            (emulator / "blocksciEmulatorAnalysis_data").mkdir()
            (emulator / "blocksciEmulatorAnalysis_data" / "unified_report.json").write_text("{}")
            external = root / "external-run"
            external.mkdir()
            write_manifest(external, {"mode": "external", "run_id": "external-run"})

            states = {state.run_dir.name: state for state in discover_runs(root)}

            self.assertEqual(states["emulator-run"].mode, "emulator")
            self.assertTrue(states["emulator-run"].stages["emulation"])
            self.assertTrue(states["emulator-run"].stages["report"])
            self.assertEqual(report_status(emulator), "diagnostics_missing")
            self.assertEqual(states["external-run"].mode, "external")

    def test_report_status_surfaces_failed_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            report_dir = run_dir / "blocksciEmulatorAnalysis_data"
            report_dir.mkdir()
            (report_dir / "unified_report.json").write_text(
                json.dumps({"integration_diagnostics": {"status": "not_ok"}})
            )
            self.assertEqual(report_status(run_dir), "diagnostics_not_ok")

    def test_report_status_surfaces_unavailable_emulator_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            report_dir = run_dir / "blocksciEmulatorAnalysis_data"
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
            self.assertFalse(stage_state(run_dir)["baseline"])
            target = run_dir / BASELINE_FILE
            target.parent.mkdir(parents=True)
            target.write_text("{}")
            self.assertTrue(stage_state(run_dir)["baseline"])

    def test_write_manifest_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_manifest(run_dir, {"mode": "external"})
            with self.assertRaises(FileExistsError):
                write_manifest(run_dir, {"mode": "external"})
