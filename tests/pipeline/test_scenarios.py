import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from client.scenarios import validate_scenario  # noqa: E402


class ScenarioValidationTests(unittest.TestCase):
    def write_scenario(self, root, data):
        path = Path(root) / "scenario.json"
        path.write_text(json.dumps(data))
        return path

    def test_joinmarket_scenario_requires_maker_and_taker(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_scenario(temp, {
                "name": "jm", "rounds": 1, "blocks": 0, "default_version": "joinmarket",
                "wallets": [{"funds": [1000], "joinmarket": {"role": "maker"}}],
            })
            with self.assertRaises(ValueError):
                validate_scenario(path, "joinmarket")

    def test_joinmarket_scenario_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_scenario(temp, {
                "name": "jm", "rounds": 1, "blocks": 0, "default_version": "joinmarket",
                "wallets": [
                    {"funds": [1000], "joinmarket": {"role": "maker"}},
                    {"funds": [2000], "joinmarket": {"role": "taker"}},
                ],
            })
            summary = validate_scenario(path, "joinmarket")
            self.assertEqual(summary["makers"], 1)
            self.assertEqual(summary["takers"], 1)

    def test_wasabi_rejects_joinmarket_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_scenario(temp, {
                "name": "bad", "rounds": 1, "blocks": 0, "default_version": "2.6.0",
                "wallets": [{"funds": [1000], "joinmarket": {"role": "maker"}}],
            })
            with self.assertRaises(ValueError):
                validate_scenario(path, "wasabi")
