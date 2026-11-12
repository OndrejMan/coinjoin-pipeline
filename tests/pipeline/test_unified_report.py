import json
import sys
import tempfile
import types
import unittest
from argparse import ArgumentTypeError
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import blocksci as _blocksci
except ImportError:
    sys.modules["blocksci"] = types.SimpleNamespace(  # type: ignore[assignment]
        Blockchain=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("BlockSci is required for export_blocksci_records integration tests.")
        ),
        heuristics=types.SimpleNamespace(),
    )
else:
    _ = _blocksci

from exporters.blocksci_export import detector as unified_report
from exporters.blocksci_export.detector import export_blocksci_records
from exporters.cli import parse_args
from exporters.common import SCHEMA_VERSION, save_json, sha256_json
from exporters.parameters import Detector  # noqa: E402
from exporters.normalization import normalize_coinjoin_analysis
from exporters.parameters import parse_min_input_count
from exporters.report_builder import build_report
from exporters.scenario import load_scenario, normalize_scenario


def coinjoin_analysis_fixture():
    return {
        "coinjoins": {
            "txA": {
                "txid": "txA",
                "broadcast_time": "2026-01-01 00:00:00.000",
                "inputs": {
                    "0": {
                        "value": 150000,
                        "address": "input-a",
                        "wallet_name": "wallet-000",
                    },
                },
                "outputs": {
                    "0": {
                        "value": 100000,
                        "address": "output-a0",
                        "wallet_name": "wallet-000",
                    },
                    "1": {
                        "value": 50000,
                        "address": "output-a1",
                        "wallet_name": "wallet-000",
                    },
                    "2": {
                        "value": 50000,
                        "address": "output-a2",
                        "wallet_name": "wallet-000",
                    },
                },
            }
        }
    }


def coinjoin_analysis_fixture_with_block_height(height):
    fixture = coinjoin_analysis_fixture()
    fixture["coinjoins"]["txA"]["block_height"] = height
    return fixture


def blocksci_fixture(txid="txA"):
    return {
        txid: {
            "txid": txid,
            "broadcast_time": "2026-01-01T00:00:00",
            "block_height": 12,
            "inputs": [
                {"index": "0", "value": 150000, "address": "input-a"},
            ],
            "outputs": [
                {"index": "0", "value": 100000, "address": "output-a0"},
                {"index": "1", "value": 50000, "address": "output-a1"},
                {"index": "2", "value": 50000, "address": "output-a2"},
            ],
            "input_count": 1,
            "output_count": 3,
            "total_input_sats": 150000,
            "total_output_sats": 200000,
            "repeated_output_denominations": {"50000": 2},
        }
    }


def scenario_fixture():
    return {
        "name": "default",
        "description": "Display-only scenario metadata",
        "category": "liquidity",
        "rounds": 1,
        "blocks": 0,
        "default_version": "2.6.0",
        "wallets": [
            {
                "funds": [200000, 50000],
                "wasabi": {"anon_score_target": 7},
            },
            {
                "funds": [3000000],
                "wasabi": {"redcoin_isolation": True},
            },
        ],
    }


class UnifiedReportTest(unittest.TestCase):
    def test_wasabi_export_uses_raw_transaction_detector(self):
        class RawChain:
            def __init__(self):
                self.calls = []

            def __len__(self):
                return 10

            def filter_coinjoin_txes_raw(self, *args):
                self.calls.append(args)
                return []

        chain = RawChain()
        fake_blocksci = types.SimpleNamespace(Blockchain=lambda _config: chain)
        with mock.patch.object(unified_report, "blocksci", fake_blocksci):
            records = export_blocksci_records(Path("/tmp/config.json"), Detector("wasabi2", None))

        self.assertEqual(records, {})
        self.assertEqual(chain.calls, [(0, 10, "wasabi2")])

    def test_parse_args_rejects_removed_test_values(self):
        with self.assertRaises(SystemExit):
            parse_args(["--run-dir", "run", "--test-values"])

    def test_min_input_count_requires_a_positive_integer_or_default(self):
        self.assertIsNone(parse_min_input_count("default"))
        self.assertEqual(parse_min_input_count("7"), 7)
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(ArgumentTypeError):
                parse_min_input_count(value)

    def test_parse_args_reports_invalid_min_input_count_as_a_cli_error(self):
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                parse_args(["--run-dir", "run", "--min-input-count", value])

    def test_build_report_exact_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "2026-05-24_15-42_default"
            run_dir.mkdir()
            scenario_path = run_dir / "coinjoin_emulator_data" / "scenario.json"
            scenario_path.parent.mkdir(parents=True)
            scenario_path.write_text(json.dumps(scenario_fixture()), encoding="utf-8")

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            scenario = load_scenario(run_dir)
            report = build_report(
                run_dir,
                coinjoin_analysis,
                blocksci_fixture(),
                Detector("wasabi2"),
                scenario,
            )

        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["run"]["id"], "2026-05-24_15-42_default")
        self.assertEqual(report["run"]["started_at"], "2026-05-24T15:42:00")
        self.assertEqual(report["run"]["scenario_name"], "default")
        self.assertEqual(report["run"]["scenario_sha256"], sha256_json(scenario_fixture()))
        self.assertEqual(report["scenario"]["wallet_count"], 2)
        self.assertEqual(report["scenario"]["total_initial_funds_sats"], 3250000)
        self.assertEqual(report["summary"]["coinjoin_analysis_coinjoins"], 1)
        self.assertEqual(report["summary"]["blocksci_detected_coinjoins"], 1)
        self.assertEqual(report["summary"]["matched_by_both"], 1)
        self.assertEqual(report["summary"]["blocksci_only"], 0)
        self.assertEqual(report["summary"]["missed_by_blocksci"], 0)
        self.assertEqual(report["summary"]["blocksci_agreement_rate"], 1.0)
        self.assertEqual(report["summary"]["coinjoin_analysis_coverage_by_blocksci"], 1.0)
        self.assertEqual(
            report["transactions"]["txA"]["comparison"],
            {
                "status": "matched_by_both",
                "field_mismatches": [],
            },
        )
        self.assertEqual(
            report["divergences"],
            {
                "missed_by_blocksci": [],
                "blocksci_only": [],
                "shared_tx_mismatches": [],
            },
        )
        self.assertEqual(
            report["summary"]["divergence_counts"],
            {
                "missed_by_blocksci": 0,
                "blocksci_only": 0,
                "shared_tx_mismatches": 0,
            },
        )

    def test_build_report_blocksci_only_and_missed_by_blocksci(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), Detector("wasabi2"))

        self.assertEqual(report["summary"]["matched_by_both"], 0)
        self.assertEqual(report["summary"]["blocksci_only"], 1)
        self.assertEqual(report["summary"]["missed_by_blocksci"], 1)
        self.assertEqual(report["summary"]["blocksci_agreement_rate"], 0.0)
        self.assertEqual(report["summary"]["coinjoin_analysis_coverage_by_blocksci"], 0.0)
        self.assertEqual(report["transactions"]["txA"]["comparison"]["status"], "missed_by_blocksci")
        self.assertEqual(report["transactions"]["txB"]["comparison"]["status"], "blocksci_only")
        self.assertEqual(report["summary"]["divergence_counts"]["missed_by_blocksci"], 1)
        self.assertEqual(report["summary"]["divergence_counts"]["blocksci_only"], 1)
        self.assertEqual(report["divergences"]["missed_by_blocksci"][0]["txid"], "txA")
        self.assertEqual(
            report["divergences"]["missed_by_blocksci"][0]["reason"],
            "coinjoin-analysis reported CoinJoin, BlockSci did not detect it",
        )
        self.assertEqual(
            report["divergences"]["missed_by_blocksci"][0]["coinjoin_analysis"]["wallets"],
            ["wallet-000"],
        )
        self.assertEqual(report["divergences"]["blocksci_only"][0]["txid"], "txB")
        self.assertIsNone(report["divergences"]["blocksci_only"][0]["coinjoin_analysis"])

    def test_build_report_field_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            blocksci_records = blocksci_fixture()
            blocksci_records["txA"]["outputs"][1]["value"] = 40000

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            report = build_report(run_dir, coinjoin_analysis, blocksci_records, Detector("wasabi2"))

        mismatches = report["transactions"]["txA"]["comparison"]["field_mismatches"]
        self.assertIn("outputs[1].value: coinjoin_analysis=50000, blocksci=40000", mismatches)
        self.assertEqual(report["summary"]["divergence_counts"]["shared_tx_mismatches"], 1)
        self.assertEqual(report["divergences"]["shared_tx_mismatches"][0]["txid"], "txA")
        self.assertEqual(
            report["divergences"]["shared_tx_mismatches"][0]["mismatch_count"],
            len(mismatches),
        )

    def test_save_json_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"

            save_json(output_path, {"b": 1, "a": {"d": 4, "c": 3}})

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                '{\n  "a": {\n    "c": 3,\n    "d": 4\n  },\n  "b": 1\n}\n',
            )

    def test_normalize_scenario(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scenario_path = Path(tmpdir) / "defaultCoinJoin.json"
            scenario = normalize_scenario(scenario_fixture(), scenario_path)

        self.assertEqual(scenario["source"], str(scenario_path))
        self.assertEqual(scenario["sha256"], sha256_json(scenario_fixture()))
        self.assertEqual(scenario["name"], "default")
        self.assertNotIn("description", scenario)
        self.assertNotIn("category", scenario)
        self.assertEqual(scenario["rounds"], 1)
        self.assertEqual(scenario["blocks"], 0)
        self.assertEqual(scenario["default_version"], "2.6.0")
        self.assertEqual(scenario["wallet_count"], 2)
        self.assertEqual(scenario["total_initial_funds_sats"], 3250000)
        self.assertEqual(
            scenario["wallets"],
            [
                {
                    "wallet_name": "wallet-000",
                    "funds": [200000, 50000],
                    "total_funds_sats": 250000,
                    "version": "2.6.0",
                    "wasabi": {"anon_score_target": 7},
                },
                {
                    "wallet_name": "wallet-001",
                    "funds": [3000000],
                    "total_funds_sats": 3000000,
                    "version": "2.6.0",
                    "wasabi": {"redcoin_isolation": True},
                },
            ],
        )

    def test_normalize_scenario_preserves_joinmarket_wallet_config(self):
        fixture = {
            "name": "joinmarket",
            "rounds": 1,
            "blocks": 0,
            "default_version": "joinmarket",
            "wallets": [
                {
                    "funds": [1000],
                    "joinmarket": {"role": "taker"},
                },
                {
                    "funds": [2000],
                    "joinmarket": {"role": "maker"},
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            scenario_path = Path(tmpdir) / "defaultJoinMarket.json"
            scenario = normalize_scenario(fixture, scenario_path)

        self.assertEqual(scenario["wallets"][0]["joinmarket"], {"role": "taker"})
        self.assertEqual(scenario["wallets"][1]["joinmarket"], {"role": "maker"})

    def test_normalize_scenario_counts_delayed_funds(self):
        fixture = {
            "name": "liquidity",
            "rounds": 1,
            "blocks": 0,
            "default_version": "2.6.0",
            "wallets": [{"funds": [1000, {"value": 500, "delay_rounds": 6}]}],
        }

        scenario = normalize_scenario(fixture, Path("liquidity.json"))

        self.assertEqual(scenario["wallets"][0]["funds"], [1000, 500])
        self.assertEqual(scenario["total_initial_funds_sats"], 1500)

    def test_load_scenario_reads_only_the_run_scenario(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            self.assertIsNone(load_scenario(run_dir))

            scenario_path = run_dir / "coinjoin_emulator_data" / "scenario.json"
            scenario_path.parent.mkdir(parents=True)
            scenario_path.write_text(json.dumps(scenario_fixture()), encoding="utf-8")
            scenario = load_scenario(run_dir)

        assert scenario is not None
        self.assertEqual(scenario["source"], str(scenario_path))
        self.assertEqual(scenario["sha256"], sha256_json(scenario_fixture()))


if __name__ == "__main__":
    unittest.main()
