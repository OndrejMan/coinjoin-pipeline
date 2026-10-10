import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from coinjoin_pipeline.execution.scenarios import validate_scenario  # noqa: E402


class ScenarioValidationTests(unittest.TestCase):
    def write_scenario(self, root, data):
        path = Path(root) / "scenario.json"
        path.write_text(json.dumps(data))
        return path

    def test_joinmarket_scenario_requires_maker_and_taker(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_scenario(
                temp,
                {
                    "name": "jm",
                    "rounds": 1,
                    "blocks": 0,
                    "default_version": "joinmarket",
                    "wallets": [{"funds": [1000], "joinmarket": {"role": "maker"}}],
                },
            )
            with self.assertRaises(ValueError):
                validate_scenario(path, "joinmarket")

    def test_joinmarket_scenario_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_scenario(
                temp,
                {
                    "name": "jm",
                    "rounds": 1,
                    "blocks": 0,
                    "default_version": "joinmarket",
                    "wallets": [
                        {"funds": [1000], "joinmarket": {"role": "maker"}},
                        {"funds": [2000], "joinmarket": {"role": "taker"}},
                    ],
                },
            )
            summary = validate_scenario(path, "joinmarket")
            self.assertEqual(summary["makers"], 1)
            self.assertEqual(summary["takers"], 1)

    def test_wasabi_rejects_joinmarket_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_scenario(
                temp,
                {
                    "name": "bad",
                    "rounds": 1,
                    "blocks": 0,
                    "default_version": "2.6.0",
                    "wallets": [{"funds": [1000], "joinmarket": {"role": "maker"}}],
                },
            )
            with self.assertRaises(ValueError):
                validate_scenario(path, "wasabi")

    def wasabi_scenario(self, root, wallet):
        return self.write_scenario(
            root,
            {"name": "liquidity", "rounds": 1, "blocks": 0, "default_version": "2.6.0", "wallets": [wallet]},
        )

    def test_delayed_funds_count_their_value(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.wasabi_scenario(temp, {"funds": [1000, {"value": 500, "delay_rounds": 3}]})
            self.assertEqual(validate_scenario(path, "wasabi")["total_funds_sats"], 1500)

    def test_delayed_fund_needs_a_positive_value(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.wasabi_scenario(temp, {"funds": [{"delay_rounds": 3}]})
            with self.assertRaisesRegex(ValueError, "positive 'value'"):
                validate_scenario(path, "wasabi")

    def test_postmix_spend_sets_exactly_one_amount(self):
        for spend in ({"delay_rounds": 2}, {"value": 10, "all": True}, {"value": 0, "all": True}):
            with self.subTest(spend=spend), tempfile.TemporaryDirectory() as temp:
                path = self.wasabi_scenario(temp, {"funds": [1000], "wasabi": {"postmix_spends": [spend]}})
                with self.assertRaisesRegex(ValueError, "either a positive 'value'"):
                    validate_scenario(path, "wasabi")

    def test_postmix_spends_are_wasabi_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.wasabi_scenario(
                temp,
                {"funds": [1000], "joinmarket": {"role": "taker"}, "wasabi": {"postmix_spends": [{"all": True}]}},
            )
            with self.assertRaisesRegex(ValueError, "Wasabi wallets only"):
                validate_scenario(path, "joinmarket")

