import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from client.research import (  # noqa: E402
    dry_run_external,
    external_analyze,
    external_command,
    require_baseline,
    require_datadir,
    resolve_false_cjtxs,
    validate_existing_run,
)


class ResearchPreflightTests(unittest.TestCase):
    def test_false_cjtxs_are_auto_discovered_next_to_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = root / "coinjoin_tx_info.json"
            baseline.write_text(json.dumps({"coinjoins": {}}))
            first = root / "false_cjtxs.json"
            second = root / "false_cjtxs.json.manual"
            first.write_text(json.dumps({"manual": ["tx-a"]}))
            second.write_text(json.dumps({"reuse": ["tx-b"]}))

            self.assertEqual(resolve_false_cjtxs(baseline, None), [first, second])

    def test_datadir_requires_blocks_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                require_datadir(root)
            (root / "blocks").mkdir()
            self.assertEqual(require_datadir(root), root.resolve())

    def test_baseline_requires_coinjoins_object(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            invalid = root / "invalid.json"
            invalid.write_text("{}")
            with self.assertRaises(ValueError):
                require_baseline(invalid)
            valid = root / "valid.json"
            valid.write_text(json.dumps({"coinjoins": {}}))
            self.assertEqual(require_baseline(valid), valid.resolve())

    def test_external_command_quotes_run_id(self):
        args = argparse.Namespace(run_id="run with spaces", network="bitcoin", coinjoin_type="wasabi2")
        self.assertIn("--run-dir 'run with spaces'", external_command(args))

    def test_external_dry_run_does_not_create_run_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            datadir = root / "bitcoin"
            (datadir / "blocks").mkdir(parents=True)
            baseline = root / "baseline.json"
            baseline.write_text(json.dumps({"coinjoins": {}}))
            args = argparse.Namespace(
                runs_root=root / "runs",
                run_id="planned-run",
                resume=False,
                bitcoin_datadir=datadir,
                baseline=baseline,
                network="bitcoin",
                coinjoin_type="wasabi2",
                runtime="docker",
            )

            with mock.patch("client.research.runtime_check"):
                dry_run_external(args)
            self.assertFalse(args.runs_root.exists())

    def test_validate_existing_run_uses_read_only_container_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            (run_dir / "coinjoin_emulator_data").mkdir(parents=True)
            target = run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json"
            target.parent.mkdir()
            target.write_text("{}")
            config = run_dir / "blocksci_data" / "config.json"
            config.parent.mkdir()
            config.write_text("{}")
            report = run_dir / "coinjoinPipeline_data" / "unified_report.json"
            report.parent.mkdir()
            report.write_text(json.dumps({
                "run_manifest": {"images": {"blocksci": "blocksci:test"}},
                "integration_diagnostics": {"status": "ok"},
            }))
            args = argparse.Namespace(runs_root=root, run_dir="run", runtime="docker", blocksci_image=None)
            with mock.patch("client.research.runtime_check"), mock.patch("client.research.subprocess.run") as run_mock:
                validate_existing_run(args)

            command = run_mock.call_args.args[0]
            self.assertIn(f"{root}:/runs/emulation/logs:ro", command)
            self.assertIn("/runs/emulation/logs/run/blocksci_data/config.json", command[-1])
            self.assertIn("blocksci:test", command)

    def test_invalid_external_preflight_does_not_create_run_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = argparse.Namespace(
                runs_root=root / "runs",
                run_id="bad-input",
                resume=False,
                bitcoin_datadir=root / "missing-datadir",
                baseline=root / "missing-baseline.json",
                network="bitcoin",
                coinjoin_type="wasabi2",
                min_free_gb=1,
                runtime="docker",
                blocksci_image="blocksci:test",
            )
            with self.assertRaises(ValueError):
                external_analyze(args)
            self.assertFalse(args.runs_root.exists())

    def test_external_analysis_passes_reproduction_command_to_container(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            datadir = root / "bitcoin"
            (datadir / "blocks").mkdir(parents=True)
            baseline = root / "baseline.json"
            baseline.write_text(json.dumps({"coinjoins": {}}))
            false_cjtxs = root / "false_cjtxs.json"
            false_cjtxs.write_text(json.dumps({"manual": ["tx-a"]}))
            args = argparse.Namespace(
                runs_root=root / "runs",
                run_id="external-run",
                resume=False,
                bitcoin_datadir=datadir,
                baseline=baseline,
                network="bitcoin",
                coinjoin_type="wasabi2",
                min_free_gb=0,
                runtime="docker",
                blocksci_image="blocksci:test",
            )

            with mock.patch("client.research.subprocess.run") as run_mock:
                external_analyze(args)

            command = run_mock.call_args.args[0]
            run_dir = root / "runs" / "external-run"
            self.assertTrue((run_dir / "coinjoin-analysis_data" / "false_cjtxs.json").is_file())
            manifest = json.loads((run_dir / "research_manifest.json").read_text())
            self.assertEqual(manifest["inputs"]["false_cjtxs"][0]["path"], str(false_cjtxs))
            self.assertIn("sha256", manifest["inputs"]["false_cjtxs"][0])
            self.assertIn("--env", command)
            self.assertIn(
                "REPRODUCTION_COMMAND=./runIt.sh external analyze --run-id external-run --resume",
                command,
            )
