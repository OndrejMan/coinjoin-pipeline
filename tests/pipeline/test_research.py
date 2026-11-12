import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from exporters.manifest import report_input_hashes  # noqa: E402
from coinjoin_pipeline.execution.research import validate_existing_run


class ResearchPreflightTests(unittest.TestCase):
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
            report.write_text(
                json.dumps(
                    {
                        "run_manifest": {
                            "images": {"blocksci": "blocksci:test"},
                            "inputs": report_input_hashes(run_dir),
                        },
                        "integration_diagnostics": {"status": "ok"},
                    }
                )
            )
            args = argparse.Namespace(runs_root=root, run_dir="run", runtime="docker", blocksci_image=None)
            with (
                mock.patch("coinjoin_pipeline.execution.research.runtime_check"),
                mock.patch("coinjoin_pipeline.execution.research.subprocess.run") as run_mock,
            ):
                validate_existing_run(args)

            command = run_mock.call_args.args[0]
            self.assertIn(f"{root}:/runs/emulation/logs:ro", command)
            self.assertIn("/runs/emulation/logs/run/blocksci_data/config.json", command[-1])
            self.assertIn("blocksci:test", command)
