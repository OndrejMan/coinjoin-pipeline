import hashlib
import json
import os
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
from exporters.emulator_data import build_emulator_data, load_wasabi_round_labels
from exporters.manifest import build_run_manifest, compare_run_manifests
from exporters.markdown_report import render_report
from exporters.parameters import Detector, ReportImages  # noqa: E402
from exporters.normalization import (
    fill_missing_block_heights,
    load_exported_block_tx_index,
    normalize_coinjoin_analysis,
)
from exporters.parameters import parse_min_input_count
from exporters.report_builder import build_report
from exporters.scenario import build_scenario_checks, load_scenario, normalize_scenario


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


def emulator_data_fixture():
    return {
        "schema_version": "1.0",
        "run_id": "run",
        "coinjoin_type": "wasabi2",
        "label_provenance": {
            "independent": True,
            "sources": ["fixture/wasabi-coordinator/Logs.txt"],
            "baseline_used_for_labels": False,
        },
        "summary": {
            "transactions": 3,
            "coinjoin_transactions": 1,
            "non_coinjoin_transactions": 2,
            "unknown_transactions": 0,
            "wallet_addresses": 4,
            "labeled_io_records": 4,
            "total_io_records": 4,
        },
        "transactions": {
            "txA": {"txid": "txA", "is_coinjoin": True, "inputs": [], "outputs": []},
            "txB": {"txid": "txB", "is_coinjoin": False, "inputs": [], "outputs": []},
            "txC": {"txid": "txC", "is_coinjoin": False, "inputs": [], "outputs": []},
        },
    }


def write_producer_label_manifest(
    raw_emulator_dir: Path,
    engine: str,
    source_names: list[str],
    *,
    complete: bool = True,
    reason: str | None = None,
    positive_count: int | None = None,
) -> None:
    data_dir = raw_emulator_dir / "data"
    sources = []
    for source_name in source_names:
        source_path = data_dir / source_name
        sources.append(
            {
                "path": source_name,
                "size_bytes": source_path.stat().st_size,
                "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            }
        )
    save_json(
        data_dir / "coinjoin_label_manifest.json",
        {
            "schema_version": "1.0",
            "engine": engine,
            "complete": complete,
            "reason": reason,
            "positive_rule": "test producer rule",
            "positive_count": positive_count,
            "sources": sources,
        },
    )


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

    def test_run_manifest_records_reproduction_command(self):
        with mock.patch.dict(
            os.environ,
            {"REPRODUCTION_COMMAND": "./runIt.sh full-run --engine joinmarket"},
        ):
            manifest = build_run_manifest(
                Path("/tmp/run"),
                None,
                Detector(
                    "joinmarket",
                    min_input_count=1,
                    first_wasabi2_block=0,
                ),
                "joinmarket",
            )
        self.assertEqual(
            manifest["execution"]["reproduction_command"],
            "./runIt.sh full-run --engine joinmarket",
        )

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

    def test_parse_args_accepts_manifest_provenance(self):
        args = parse_args(
            [
                "--run-dir",
                "run",
                "--blocksci-image",
                "blocksci:test",
                "--coinjoin-analysis-image",
                "coinjoin-analysis:test",
                "--coinjoin-emulator-image",
                "coinjoin-emulator:test",
                "--unified-report-image",
                "python:3.12-slim-bookworm",
                "--emulator-git-commit",
                "abc123",
            ]
        )

        self.assertEqual(args.blocksci_image, "blocksci:test")
        self.assertEqual(args.coinjoin_analysis_image, "coinjoin-analysis:test")
        self.assertEqual(args.coinjoin_emulator_image, "coinjoin-emulator:test")
        self.assertEqual(args.unified_report_image, "python:3.12-slim-bookworm")
        self.assertEqual(args.emulator_git_commit, "abc123")

    def test_run_manifest_comparison_reports_changed_provenance(self):
        previous = build_run_manifest(
            Path("/tmp/run"),
            {"name": "scenario-a", "sha256": "old-hash"},
            Detector(
                "joinmarket",
                min_input_count=1,
                first_wasabi2_block=0,
            ),
            "joinmarket",
            emulator_git_commit="old-commit",
            images=ReportImages(
                references={
                    "blocksci": "blocksci:old",
                    "coinjoin_analysis": "coinjoin-analysis:old",
                    "coinjoin_emulator": "coinjoin-emulator:old",
                }
            ),
        )
        current = build_run_manifest(
            Path("/tmp/run"),
            {"name": "scenario-a", "sha256": "new-hash"},
            Detector(
                "joinmarket",
                min_input_count=2,
                first_wasabi2_block=0,
            ),
            "joinmarket",
            emulator_git_commit="old-commit",
            images=ReportImages(
                references={
                    "blocksci": "blocksci:new",
                    "coinjoin_analysis": "coinjoin-analysis:old",
                    "coinjoin_emulator": "coinjoin-emulator:old",
                }
            ),
        )

        comparison = compare_run_manifests(previous, current)

        self.assertTrue(comparison["available"])
        self.assertFalse(comparison["matches"])
        changed_fields = {item["field"] for item in comparison["differences"]}
        self.assertEqual(changed_fields, {"scenario.sha256", "detector", "images.blocksci"})

    def test_build_report_compares_previous_run_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            previous_manifest = build_run_manifest(
                run_dir,
                {"name": "default", "sha256": "old-hash"},
                Detector(
                    "wasabi2",
                    min_input_count=1,
                    first_wasabi2_block=0,
                ),
                "wasabi",
            )
            report = build_report(
                run_dir,
                coinjoin_analysis,
                blocksci_fixture(),
                Detector("wasabi2", min_input_count=1),
                {"name": "default", "sha256": "new-hash"},
                previous_run_manifest=previous_manifest,
            )

        comparison = report["run_manifest_comparison"]
        self.assertTrue(comparison["available"])
        self.assertFalse(comparison["matches"])
        self.assertEqual(comparison["differences"][0]["field"], "scenario.sha256")

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
                emulator_data=emulator_data_fixture(),
            )

        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["run"]["id"], "2026-05-24_15-42_default")
        self.assertEqual(report["run"]["started_at"], "2026-05-24T15:42:00")
        self.assertEqual(report["run"]["scenario_name"], "default")
        self.assertEqual(report["run"]["scenario_sha256"], sha256_json(scenario_fixture()))
        self.assertEqual(
            report["run_manifest"]["scenario"]["sha256"],
            sha256_json(scenario_fixture()),
        )
        self.assertEqual(report["run_manifest"]["execution"]["engine"], "wasabi")
        self.assertEqual(report["run_manifest"]["execution"]["coinjoin_type"], "wasabi2")
        self.assertEqual(report["run_manifest"]["detector"]["blocksci_min_input_count"], None)
        self.assertFalse(report["run_manifest_comparison"]["available"])
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
            report["summary"]["scenario_checks"],
            {
                "scenario_wallet_count": 2,
                "coinjoin_analysis_wallet_count": 1,
                "wallet_count_rule": "exact",
                "wallet_count_matches": False,
                "coinjoin_analysis_input_sats": 150000,
                "max_coinjoin_input_sats": 150000,
                "scenario_initial_funds_sats": 3250000,
                "input_sats_within_scenario_funds": True,
                "coinjoin_input_sats_to_funds_ratio": 0.046,
                "per_wallet_observed_counts": {
                    "wallet-000": {"input_count": 1, "output_count": 3},
                },
            },
        )
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
        self.assertEqual(report["summary"]["emulator_data_transactions"], 3)
        self.assertEqual(report["summary"]["emulator_data_coinjoins"], 1)
        self.assertEqual(
            report["detector_evaluations"]["blocksci"],
            {
                "evaluated_transactions": 3,
                "detected_transactions": 1,
                "detected_in_scope": 1,
                "out_of_scope_detected": 0,
                "true_positives": 1,
                "false_positives": 0,
                "true_negatives": 2,
                "false_negatives": 0,
                "unknown": 0,
                "precision": 1.0,
                "recall": 1.0,
                "f1": 1.0,
                "specificity": 1.0,
                "false_positive_rate": 0.0,
                "false_positive_txids": [],
                "false_negative_txids": [],
                "out_of_scope_detected_txids": [],
            },
        )
        self.assertEqual(
            report["detector_evaluations"]["coinjoin_analysis"],
            report["detector_evaluations"]["blocksci"],
        )

    def test_build_report_includes_image_digests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            report = build_report(
                run_dir,
                normalize_coinjoin_analysis(coinjoin_analysis_fixture()),
                {},
                Detector("joinmarket"),
                images=ReportImages(
                    digests={
                        "blocksci": "sha256:blocksci",
                        "coinjoin_analysis": "sha256:analysis",
                        "coinjoin_emulator": "sha256:emulator",
                    }
                ),
            )

        self.assertEqual(report["run_manifest"]["image_digests"]["blocksci"], "sha256:blocksci")
        self.assertEqual(
            report["run_manifest"]["image_digests"]["coinjoin_analysis"],
            "sha256:analysis",
        )
        self.assertEqual(
            report["run_manifest"]["image_digests"]["coinjoin_emulator"],
            "sha256:emulator",
        )

    def test_build_emulator_data_labels_chain_transactions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            txid = "a" * 64
            coordinator_dir = run_dir / "coinjoin_emulator_data" / "data" / "wasabi-coordinator"
            coordinator_dir.mkdir(parents=True)
            (coordinator_dir / "Logs.txt").write_text(
                f"2026-01-01 00:00:00 [INFO] Round (abc): Successfully broadcast the coinjoin: {txid}.\n",
                encoding="utf-8",
            )
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "wasabi",
                ["wasabi-coordinator/Logs.txt"],
            )
            (block_dir / "block_1.json").write_text(
                json.dumps(
                    {
                        "height": 1,
                        "tx": [
                            {
                                "txid": "coinbase",
                                "vin": [{"coinbase": "00"}],
                                "vout": [],
                            },
                            {
                                "txid": "funding",
                                "vin": [{"txid": "coinbase", "vout": 0}],
                                "vout": [
                                    {
                                        "value": 0.0015,
                                        "n": 0,
                                        "scriptPubKey": {
                                            "address": "input-a",
                                            "type": "witness_v0_keyhash",
                                        },
                                    }
                                ],
                            },
                            {
                                "txid": txid,
                                "vin": [{"txid": "funding", "vout": 0}],
                                "vout": [
                                    {
                                        "value": 0.001,
                                        "n": 0,
                                        "scriptPubKey": {
                                            "address": "output-a0",
                                            "type": "witness_v0_keyhash",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            data = coinjoin_analysis_fixture()

            emulator_data = build_emulator_data(run_dir, data, "wasabi2")

        self.assertEqual(emulator_data["summary"]["transactions"], 2)
        self.assertEqual(emulator_data["summary"]["coinjoin_transactions"], 1)
        self.assertEqual(emulator_data["summary"]["non_coinjoin_transactions"], 1)
        self.assertFalse(emulator_data["transactions"]["funding"]["is_coinjoin"])
        self.assertTrue(emulator_data["transactions"][txid]["is_coinjoin"])
        self.assertEqual(emulator_data["transactions"][txid]["round_id"], "abc")
        self.assertEqual(
            emulator_data["transactions"][txid]["inputs"][0]["wallet_name"],
            "wallet-000",
        )
        self.assertEqual(
            emulator_data["transactions"][txid]["outputs"][0]["wallet_name"],
            "wallet-000",
        )
        self.assertTrue(emulator_data["label_provenance"]["independent"])
        self.assertFalse(emulator_data["label_provenance"]["baseline_used_for_labels"])

    def test_wasabi_broadcast_parser_accepts_legacy_format_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            log_path = run_dir / "wasabi-backend" / "Logs.txt"
            log_path.parent.mkdir()
            txid = "A" * 64
            log_path.write_text(
                "2026-01-01 00:00:00 [14]\tINFO\tLegacy.Backend\tROUND (legacy-id): "
                f"Successfully broadcasted coinjoin transaction: {txid}\n",
                encoding="utf-8",
            )

            labels = load_wasabi_round_labels(run_dir, [log_path])

        self.assertEqual(labels[0]["round_id"], "legacy-id")
        self.assertEqual(labels[0]["txid"], txid.lower())

    def test_wasabi_broadcast_parser_accepts_no_round_prefix_and_trailing_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            log_path = run_dir / "wasabi-coordinator" / "Logs.txt"
            log_path.parent.mkdir()
            txid = "b" * 64
            log_path.write_text(
                f"Successfully broadcast the coinjoin: {txid} after retry\n",
                encoding="utf-8",
            )

            labels = load_wasabi_round_labels(run_dir, [log_path])

        self.assertIsNone(labels[0]["round_id"])
        self.assertEqual(labels[0]["txid"], txid)

    def test_build_emulator_data_leaves_labels_unknown_without_producer_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            save_json(
                block_dir / "block_1.json",
                {
                    "height": 1,
                    "tx": [
                        {
                            "txid": "txA",
                            "vin": [{"txid": "funding", "vout": 0}],
                            "vout": [],
                        }
                    ],
                },
            )

            emulator_data = build_emulator_data(run_dir, coinjoin_analysis_fixture(), "wasabi2")
            report = build_report(
                run_dir,
                normalize_coinjoin_analysis(coinjoin_analysis_fixture()),
                blocksci_fixture(),
                Detector("wasabi2"),
                emulator_data=emulator_data,
            )

        self.assertIsNone(emulator_data["transactions"]["txA"]["is_coinjoin"])
        self.assertEqual(emulator_data["summary"]["unknown_transactions"], 1)
        self.assertFalse(emulator_data["label_provenance"]["independent"])
        self.assertNotIn("detection_confusion_matrix", report)
        self.assertIsNone(report["detector_evaluations"])
        self.assertEqual(report["evaluation_scope"], "emulator_labels_unavailable")
        self.assertIn(
            "Independent emulator producer labels were unavailable",
            render_report(report),
        )

    def test_verified_empty_producer_source_labels_transactions_negative(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            data_dir = run_dir / "coinjoin_emulator_data" / "data"
            block_dir = data_dir / "btc-node"
            coordinator_dir = data_dir / "wasabi-coordinator"
            block_dir.mkdir(parents=True)
            coordinator_dir.mkdir(parents=True)
            (coordinator_dir / "Logs.txt").write_text("", encoding="utf-8")
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "wasabi",
                ["wasabi-coordinator/Logs.txt"],
            )
            save_json(
                block_dir / "block_1.json",
                {
                    "height": 1,
                    "tx": [
                        {
                            "txid": "txA",
                            "vin": [{"txid": "funding", "vout": 0}],
                            "vout": [],
                        }
                    ],
                },
            )

            emulator_data = build_emulator_data(
                run_dir,
                coinjoin_analysis_fixture(),
                "wasabi2",
            )

        self.assertTrue(emulator_data["label_provenance"]["independent"])
        self.assertFalse(emulator_data["transactions"]["txA"]["is_coinjoin"])
        self.assertEqual(emulator_data["summary"]["non_coinjoin_transactions"], 1)

    def test_empty_wasabi_parse_result_with_candidate_transaction_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            data_dir = run_dir / "coinjoin_emulator_data" / "data"
            block_dir = data_dir / "btc-node"
            coordinator_dir = data_dir / "wasabi-backend"
            block_dir.mkdir(parents=True)
            coordinator_dir.mkdir(parents=True)
            (coordinator_dir / "Logs.txt").write_text(
                "legacy backend says the round completed in an unknown format\n",
                encoding="utf-8",
            )
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "wasabi",
                ["wasabi-backend/Logs.txt"],
            )
            save_json(
                block_dir / "block_1.json",
                {
                    "height": 1,
                    "tx": [
                        {
                            "txid": "candidate",
                            "vin": [{"txid": f"funding-{index}", "vout": 0} for index in range(5)],
                            "vout": [],
                        }
                    ],
                },
            )

            emulator_data = build_emulator_data(
                run_dir,
                coinjoin_analysis_fixture(),
                "wasabi2",
            )

        self.assertFalse(emulator_data["label_provenance"]["independent"])
        self.assertIn(
            "no parseable broadcast records",
            emulator_data["label_provenance"]["unavailable_reason"],
        )
        self.assertIsNone(emulator_data["transactions"]["candidate"]["is_coinjoin"])
        self.assertEqual(
            emulator_data["summary"]["wasabi_parseability_candidate_txids"],
            ["candidate"],
        )

    def test_manifest_zero_positive_count_allows_high_input_non_coinjoin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            data_dir = run_dir / "coinjoin_emulator_data" / "data"
            block_dir = data_dir / "btc-node"
            coordinator_dir = data_dir / "wasabi-coordinator"
            block_dir.mkdir(parents=True)
            coordinator_dir.mkdir(parents=True)
            (coordinator_dir / "Logs.txt").write_text("no successful rounds\n", encoding="utf-8")
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "wasabi",
                ["wasabi-coordinator/Logs.txt"],
                positive_count=0,
            )
            save_json(
                block_dir / "block_1.json",
                {
                    "height": 1,
                    "tx": [
                        {
                            "txid": "batch-payment",
                            "vin": [{"txid": f"funding-{index}", "vout": 0} for index in range(5)],
                            "vout": [],
                        }
                    ],
                },
            )

            emulator_data = build_emulator_data(run_dir, coinjoin_analysis_fixture(), "wasabi2")

        self.assertTrue(emulator_data["label_provenance"]["independent"])
        self.assertFalse(emulator_data["transactions"]["batch-payment"]["is_coinjoin"])

    def test_manifest_positive_count_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            data_dir = run_dir / "coinjoin_emulator_data" / "data"
            block_dir = data_dir / "btc-node"
            coordinator_dir = data_dir / "wasabi-coordinator"
            block_dir.mkdir(parents=True)
            coordinator_dir.mkdir(parents=True)
            (coordinator_dir / "Logs.txt").write_text("unrecognized record\n", encoding="utf-8")
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "wasabi",
                ["wasabi-coordinator/Logs.txt"],
                positive_count=1,
            )

            emulator_data = build_emulator_data(run_dir, coinjoin_analysis_fixture(), "wasabi2")

        self.assertFalse(emulator_data["label_provenance"]["independent"])
        self.assertIn(
            "does not match manifest",
            emulator_data["label_provenance"]["unavailable_reason"],
        )

    def test_unmatched_producer_positive_fails_closed_and_is_rendered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            data_dir = run_dir / "coinjoin_emulator_data" / "data"
            block_dir = data_dir / "btc-node"
            coordinator_dir = data_dir / "wasabi-coordinator"
            block_dir.mkdir(parents=True)
            coordinator_dir.mkdir(parents=True)
            missing_txid = "a" * 64
            (coordinator_dir / "Logs.txt").write_text(
                f"2026-01-01 00:00:00 [INFO] Round (abc): Successfully broadcast the coinjoin: {missing_txid}.\n",
                encoding="utf-8",
            )
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "wasabi",
                ["wasabi-coordinator/Logs.txt"],
            )
            save_json(
                block_dir / "block_1.json",
                {
                    "height": 1,
                    "tx": [
                        {
                            "txid": "exported-tx",
                            "vin": [{"txid": "funding", "vout": 0}],
                            "vout": [],
                        }
                    ],
                },
            )

            emulator_data = build_emulator_data(run_dir, coinjoin_analysis_fixture(), "wasabi2")
            report = build_report(run_dir, {}, {}, Detector("wasabi2"), emulator_data=emulator_data)

        self.assertFalse(emulator_data["label_provenance"]["independent"])
        self.assertEqual(emulator_data["summary"]["unmatched_positive_txids"], [missing_txid])
        self.assertIsNone(emulator_data["transactions"]["exported-tx"]["is_coinjoin"])
        self.assertEqual(report["evaluation_scope"], "emulator_labels_unavailable")
        self.assertIn("producer-positive transactions are missing", render_report(report))

    def test_modified_producer_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            data_dir = run_dir / "coinjoin_emulator_data" / "data"
            block_dir = data_dir / "btc-node"
            coordinator_dir = data_dir / "wasabi-coordinator"
            block_dir.mkdir(parents=True)
            coordinator_dir.mkdir(parents=True)
            log_path = coordinator_dir / "Logs.txt"
            log_path.write_text("complete capture\n", encoding="utf-8")
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "wasabi",
                ["wasabi-coordinator/Logs.txt"],
            )
            log_path.write_text("truncated\n", encoding="utf-8")
            save_json(
                block_dir / "block_1.json",
                {
                    "height": 1,
                    "tx": [
                        {
                            "txid": "txA",
                            "vin": [{"txid": "funding", "vout": 0}],
                            "vout": [],
                        }
                    ],
                },
            )

            emulator_data = build_emulator_data(
                run_dir,
                coinjoin_analysis_fixture(),
                "wasabi2",
            )

        self.assertFalse(emulator_data["label_provenance"]["independent"])
        self.assertIn(
            "does not match manifest",
            emulator_data["label_provenance"]["unavailable_reason"],
        )
        self.assertIsNone(emulator_data["transactions"]["txA"]["is_coinjoin"])

    def test_build_emulator_data_adds_joinmarket_round_labels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            (run_dir / "coinjoin_emulator_data" / "data" / "joinmarket_round_events.json").write_text(
                json.dumps(
                    [
                        {
                            "round_id": 1,
                            "status": "confirmed",
                            "taker": "jcs-000",
                            "candidate_makers": ["jcs-001", "jcs-002"],
                            "destination_address": "output-a0",
                            "destination_matches": [{"txid": "txA", "block_height": 0}],
                        },
                        {
                            "round_id": 2,
                            "status": "failed",
                            "destination_address": "failed-destination",
                            "txid": "txB",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "joinmarket",
                ["joinmarket_round_events.json"],
            )
            save_json(
                block_dir / "block_0.json",
                {
                    "height": 0,
                    "tx": [
                        {
                            "txid": "funding",
                            "vin": [{"coinbase": "00"}],
                            "vout": [
                                {
                                    "n": 0,
                                    "value": 0.0015,
                                    "scriptPubKey": {"address": "input-a"},
                                },
                            ],
                        },
                        {
                            "txid": "txA",
                            "vin": [{"txid": "funding", "vout": 0}],
                            "vout": [
                                {
                                    "n": 0,
                                    "value": 0.001,
                                    "scriptPubKey": {"address": "output-a0"},
                                },
                                {
                                    "n": 1,
                                    "value": 0.0005,
                                    "scriptPubKey": {"address": "output-a1"},
                                },
                            ],
                        },
                        {
                            "txid": "txB",
                            "vin": [{"txid": "funding", "vout": 0}],
                            "vout": [
                                {
                                    "n": 0,
                                    "value": 0.001,
                                    "scriptPubKey": {"address": "failed-destination"},
                                },
                            ],
                        },
                    ],
                },
            )

            emulator_data = build_emulator_data(run_dir, coinjoin_analysis_fixture(), "joinmarket")

        tx = emulator_data["transactions"]["txA"]
        self.assertEqual(tx["taker"], "jcs-000")
        self.assertEqual(tx["candidate_makers"], ["jcs-001", "jcs-002"])
        self.assertEqual(tx["round_id"], "1")
        self.assertEqual(tx["input_owners"], ["wallet-000"])
        self.assertEqual(tx["output_owners"], ["wallet-000"])
        self.assertFalse(emulator_data["transactions"]["txB"]["is_coinjoin"])

    def test_build_emulator_data_reads_reconciled_joinmarket_round_labels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            label_path = run_dir / "coinjoin_emulator_data" / "data" / "joinmarket_round_events.json"
            save_json(
                label_path,
                [
                    {
                        "round_id": 1,
                        "status": "confirmed",
                        "taker": "jcs-000",
                        "destination_matches": [{"txid": "txA", "block_height": 7}],
                    }
                ],
            )
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "joinmarket",
                ["joinmarket_round_events.json"],
                positive_count=1,
            )
            save_json(
                block_dir / "block_7.json",
                {
                    "height": 7,
                    "tx": [
                        {"txid": "funding", "vin": [{"coinbase": "00"}], "vout": []},
                        {
                            "txid": "txA",
                            "vin": [{"txid": "funding", "vout": 0}],
                            "vout": [],
                        },
                    ],
                },
            )

            emulator_data = build_emulator_data(run_dir, coinjoin_analysis_fixture(), "joinmarket")

        self.assertTrue(emulator_data["label_provenance"]["independent"])
        self.assertTrue(emulator_data["transactions"]["txA"]["is_coinjoin"])
        self.assertEqual(emulator_data["summary"]["producer_positive_labels"], 1)

    def test_joinmarket_destination_conflicts_disable_all_labels(self):
        cases = [
            ("multiple_matches", ["txA", "txB"]),
            ("duplicate_destination", []),
            ("duplicate_destination", ["txA"]),
            ("duplicate_destination", ["txA", "txB"]),
        ]
        for status, txids in cases:
            with (
                self.subTest(status=status, txids=txids),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                run_dir = Path(tmpdir)
                data_dir = run_dir / "coinjoin_emulator_data" / "data"
                block_dir = data_dir / "btc-node"
                block_dir.mkdir(parents=True)
                save_json(
                    data_dir / "joinmarket_round_events.json",
                    [
                        {
                            "export_round_id": 1,
                            "status": status,
                            "destination_matches": [{"txid": txid, "block_height": 7} for txid in txids],
                        },
                        {
                            "export_round_id": 2,
                            "status": "confirmed",
                            "destination_matches": [{"txid": "txC", "block_height": 7}],
                        },
                    ],
                )
                # Even a hash-valid manifest with a matching positive count cannot
                # declare a capture complete when a round has conflicting evidence.
                write_producer_label_manifest(
                    run_dir / "coinjoin_emulator_data",
                    "joinmarket",
                    ["joinmarket_round_events.json"],
                    positive_count=1,
                )
                save_json(
                    block_dir / "block_7.json",
                    {
                        "height": 7,
                        "tx": [
                            {
                                "txid": txid,
                                "vin": [{"txid": "funding", "vout": 0}],
                                "vout": [],
                            }
                            for txid in ("txA", "txB", "txC")
                        ],
                    },
                )

                emulator_data = build_emulator_data(run_dir, coinjoin_analysis_fixture(), "joinmarket")

                self.assertFalse(emulator_data["label_provenance"]["independent"])
                self.assertIn(status, emulator_data["label_provenance"]["unavailable_reason"])
                self.assertEqual(len(emulator_data["transactions"]), 3)
                self.assertTrue(all(tx["is_coinjoin"] is None for tx in emulator_data["transactions"].values()))

    def test_build_emulator_data_rejects_malformed_joinmarket_label_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            label_path = run_dir / "coinjoin_emulator_data" / "data" / "joinmarket_round_events.json"
            label_path.parent.mkdir(parents=True)
            save_json(label_path, {"round_id": 1})
            write_producer_label_manifest(
                run_dir / "coinjoin_emulator_data",
                "joinmarket",
                ["joinmarket_round_events.json"],
            )

            emulator_data = build_emulator_data(
                run_dir,
                coinjoin_analysis_fixture(),
                "joinmarket",
            )

        self.assertFalse(emulator_data["label_provenance"]["independent"])
        self.assertIn(
            "cannot be parsed",
            emulator_data["label_provenance"]["unavailable_reason"],
        )

    def test_detection_confusion_matrix_counts_false_positive_and_false_negative(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            report = build_report(
                run_dir,
                coinjoin_analysis,
                blocksci_fixture("txB"),
                Detector("wasabi2"),
                emulator_data=emulator_data_fixture(),
            )

        matrix = report["detector_evaluations"]["blocksci"]
        self.assertEqual(matrix["true_positives"], 0)
        self.assertEqual(matrix["false_positives"], 1)
        self.assertEqual(matrix["false_negatives"], 1)
        self.assertEqual(matrix["true_negatives"], 1)
        self.assertEqual(matrix["precision"], 0.0)
        self.assertEqual(matrix["recall"], 0.0)
        self.assertEqual(matrix["false_positive_txids"], ["txB"])
        self.assertEqual(matrix["false_negative_txids"], ["txA"])

    def test_detector_evaluations_compare_both_analyzers_to_same_emulator_truth(self):
        report = build_report(
            Path("/tmp/run"),
            {**blocksci_fixture("txA"), **blocksci_fixture("txC")},
            blocksci_fixture("txB"),
            Detector("wasabi2"),
            emulator_data=emulator_data_fixture(),
        )

        blocksci_matrix = report["detector_evaluations"]["blocksci"]
        baseline_matrix = report["detector_evaluations"]["coinjoin_analysis"]
        self.assertEqual(
            (
                blocksci_matrix["true_positives"],
                blocksci_matrix["false_positives"],
                blocksci_matrix["true_negatives"],
                blocksci_matrix["false_negatives"],
            ),
            (0, 1, 1, 1),
        )
        self.assertEqual(
            (
                baseline_matrix["true_positives"],
                baseline_matrix["false_positives"],
                baseline_matrix["true_negatives"],
                baseline_matrix["false_negatives"],
            ),
            (1, 1, 1, 0),
        )
        self.assertEqual(blocksci_matrix["false_positive_txids"], ["txB"])
        self.assertEqual(blocksci_matrix["false_negative_txids"], ["txA"])
        self.assertEqual(baseline_matrix["false_positive_txids"], ["txC"])
        self.assertEqual(baseline_matrix["false_negative_txids"], [])
        self.assertNotIn("detection_confusion_matrix", report)

        markdown = render_report(report)
        self.assertIn("| BlockSci | 0 | 1 | 1 | 1 |", markdown)
        self.assertIn("| coinjoin-analysis | 1 | 1 | 1 | 0 |", markdown)

    def test_detector_evaluation_reports_detections_outside_emulator_universe(self):
        report = build_report(
            Path("/tmp/run"),
            blocksci_fixture("baseline-outside"),
            blocksci_fixture("blocksci-outside"),
            Detector("wasabi2"),
            emulator_data=emulator_data_fixture(),
        )

        blocksci_matrix = report["detector_evaluations"]["blocksci"]
        baseline_matrix = report["detector_evaluations"]["coinjoin_analysis"]
        self.assertEqual(blocksci_matrix["evaluated_transactions"], 3)
        self.assertEqual(blocksci_matrix["detected_transactions"], 1)
        self.assertEqual(blocksci_matrix["detected_in_scope"], 0)
        self.assertEqual(blocksci_matrix["out_of_scope_detected"], 1)
        self.assertEqual(
            blocksci_matrix["out_of_scope_detected_txids"],
            ["blocksci-outside"],
        )
        self.assertEqual(
            baseline_matrix["out_of_scope_detected_txids"],
            ["baseline-outside"],
        )
        self.assertEqual(
            blocksci_matrix["true_positives"]
            + blocksci_matrix["false_positives"]
            + blocksci_matrix["true_negatives"]
            + blocksci_matrix["false_negatives"]
            + blocksci_matrix["unknown"],
            blocksci_matrix["evaluated_transactions"],
        )
        self.assertIn("outside the exported emulator transaction universe", render_report(report))

    def test_detector_evaluation_excludes_unknown_labels_from_classification_counts(
        self,
    ):
        emulator_data = emulator_data_fixture()
        emulator_data["transactions"]["txC"]["is_coinjoin"] = None
        emulator_data["summary"]["non_coinjoin_transactions"] = 1
        emulator_data["summary"]["unknown_transactions"] = 1
        report = build_report(
            Path("/tmp/run"), blocksci_fixture("txC"), {}, Detector("wasabi2"), emulator_data=emulator_data
        )

        matrix = report["detector_evaluations"]["coinjoin_analysis"]
        self.assertEqual(matrix["unknown"], 1)
        self.assertEqual(matrix["false_positives"], 0)
        self.assertEqual(matrix["false_negatives"], 1)
        self.assertEqual(matrix["true_negatives"], 1)
        self.assertEqual(matrix["precision"], None)
        self.assertEqual(matrix["recall"], 0.0)

    def test_detector_evaluation_uses_none_for_rates_with_empty_denominators(self):
        emulator_data = emulator_data_fixture()
        for transaction in emulator_data["transactions"].values():
            transaction["is_coinjoin"] = False
        emulator_data["summary"]["coinjoin_transactions"] = 0
        emulator_data["summary"]["non_coinjoin_transactions"] = 3
        report = build_report(Path("/tmp/run"), {}, {}, Detector("wasabi2"), emulator_data=emulator_data)

        matrix = report["detector_evaluations"]["coinjoin_analysis"]
        self.assertIsNone(matrix["precision"])
        self.assertIsNone(matrix["recall"])
        self.assertIsNone(matrix["f1"])
        self.assertEqual(matrix["specificity"], 1.0)
        self.assertEqual(matrix["false_positive_rate"], 0.0)

    def test_report_warns_when_production_thresholds_detect_no_regtest_wasabi(self):
        emulator_data = emulator_data_fixture()
        for transaction in emulator_data["transactions"].values():
            transaction["block_height"] = 100
        report = build_report(Path("/tmp/run"), {}, {}, Detector("wasabi2"), emulator_data=emulator_data)

        self.assertEqual(
            report["warnings"][0]["code"],
            "wasabi_production_threshold_zero_detections",
        )
        self.assertIn("production minimum-input threshold", render_report(report))

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

    def test_fill_missing_block_height_from_exported_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            (block_dir / "block_226.json").write_text(
                json.dumps(
                    {
                        "height": 226,
                        "tx": [
                            {"txid": "coinbase"},
                            {"txid": "txA"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            fill_missing_block_heights(coinjoin_analysis, load_exported_block_tx_index(run_dir))
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), Detector("wasabi2"))

        tx_record = report["transactions"]["txA"]["coinjoin_analysis"]
        self.assertEqual(tx_record["block_height"], 226)
        self.assertTrue(tx_record["block_height_inferred"])
        self.assertEqual(
            report["divergences"]["missed_by_blocksci"][0]["coinjoin_analysis"]["block_height"],
            226,
        )
        self.assertTrue(report["divergences"]["missed_by_blocksci"][0]["coinjoin_analysis"]["block_height_inferred"])
        markdown = render_report(report)
        self.assertIn(
            "| missed_by_blocksci | [txA](http://localhost:3002/tx/txA) | "
            "[_226](http://localhost:3002/block-height/226) |",
            markdown,
        )
        self.assertIn("## Notable Divergences", markdown)
        self.assertIn("- tx: [txA](http://localhost:3002/tx/txA)", markdown)
        self.assertIn("#### Inputs", markdown)
        self.assertIn("| 0 | 150,000 | input-a | - | - | wallet-000 |", markdown)
        self.assertIn("#### Outputs", markdown)
        self.assertIn("| 0 | 100,000 | output-a0 | - | - | wallet-000 |", markdown)

    def test_existing_block_height_is_not_overwritten_by_exported_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            (block_dir / "block_226.json").write_text(
                json.dumps({"height": 226, "tx": [{"txid": "txA"}]}),
                encoding="utf-8",
            )

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture_with_block_height(12))
            fill_missing_block_heights(coinjoin_analysis, load_exported_block_tx_index(run_dir))
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), Detector("wasabi2"))

        tx_record = report["transactions"]["txA"]["coinjoin_analysis"]
        self.assertEqual(tx_record["block_height"], 12)
        self.assertNotIn("block_height_inferred", tx_record)
        markdown = render_report(report)
        self.assertIn(
            "| missed_by_blocksci | [txA](http://localhost:3002/tx/txA) | "
            "[12](http://localhost:3002/block-height/12) |",
            markdown,
        )
        self.assertNotIn("| _12 |", markdown)

    def test_exported_block_script_metadata_maps_taproot_to_witness_unknown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            (block_dir / "block_226.json").write_text(
                json.dumps(
                    {
                        "height": 226,
                        "tx": [
                            {
                                "txid": "prevA",
                                "vout": [
                                    {
                                        "value": 0.0015,
                                        "n": 0,
                                        "scriptPubKey": {
                                            "asm": "0 1111111111111111111111111111111111111111",
                                            "hex": "00141111111111111111111111111111111111111111111111",
                                            "address": "input-a",
                                            "type": "witness_v0_keyhash",
                                        },
                                    }
                                ],
                            },
                            {
                                "txid": "txA",
                                "vin": [{"txid": "prevA", "vout": 0}],
                                "vout": [
                                    {
                                        "value": 0.001,
                                        "n": 0,
                                        "scriptPubKey": {
                                            "asm": "0 2222222222222222222222222222222222222222",
                                            "hex": "00142222222222222222222222222222222222222222222222",
                                            "address": "output-a0",
                                            "type": "witness_v0_keyhash",
                                        },
                                    },
                                    {
                                        "value": 0.0005,
                                        "n": 1,
                                        "scriptPubKey": {
                                            "asm": "1 3333333333333333333333333333333333333333333333333333333333333333",
                                            "hex": (
                                                "51203333333333333333333333333333333333333333333333333333333333333333"
                                            ),
                                            "address": (
                                                "bcrt1pxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxvenxves3r4aqc"
                                            ),
                                            "type": "witness_v1_taproot",
                                        },
                                    },
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture_with_block_height(226))
            report = build_report(
                run_dir, coinjoin_analysis, blocksci_fixture("txB"), Detector("wasabi2", min_input_count=1)
            )

        tx_record = report["transactions"]["txA"]["coinjoin_analysis"]
        self.assertEqual(tx_record["inputs"][0]["script_type"], "witness_v0_keyhash")
        self.assertEqual(tx_record["inputs"][0]["address_type"], "WITNESS_PUBKEYHASH")
        self.assertEqual(tx_record["outputs"][0]["address_type"], "WITNESS_PUBKEYHASH")
        self.assertEqual(tx_record["outputs"][1]["script_type"], "witness_v1_taproot")
        self.assertEqual(tx_record["outputs"][1]["address_type"], "WITNESS_UNKNOWN")

        markdown = render_report(report)
        self.assertIn(
            "| 1 | 50,000 | output-a1 | witness_v1_taproot | WITNESS_UNKNOWN |",
            markdown,
        )

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

    def test_render_markdown_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), Detector("wasabi2"))

        markdown = render_report(report)

        self.assertIn("# BlockSci vs Emulator CoinJoin Report", markdown)
        self.assertIn("| run time | - |", markdown)
        self.assertIn("| coinjoin-analysis coinjoins | 1 |", markdown)
        self.assertIn("## Divergence Overview", markdown)
        self.assertIn("| missed_by_blocksci | [txA](http://localhost:3002/tx/txA) |", markdown)
        self.assertIn("| blocksci_only | [txB](http://localhost:3002/tx/txB) |", markdown)
        self.assertIn("## Missed By BlockSci", markdown)
        self.assertIn("## Missed By BlockSci Details", markdown)
        self.assertIn("#### Inputs", markdown)
        self.assertIn("#### Outputs", markdown)
        self.assertIn("wallet-000", markdown)

    def test_render_io_details_links_prev_txids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            fixture = coinjoin_analysis_fixture()
            fixture["coinjoins"]["txA"]["inputs"]["0"]["txid"] = (
                "4661b71b085395008966182375dd3b74d3b0c2f2617bfade403520da616bdb37"
            )
            coinjoin_analysis = normalize_coinjoin_analysis(fixture)
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), Detector("wasabi2"))

        markdown = render_report(report)

        self.assertIn(
            "[4661b71b...616bdb37](http://localhost:3002/tx/"
            "4661b71b085395008966182375dd3b74d3b0c2f2617bfade403520da616bdb37)",
            markdown,
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


class ScenarioFundsCheckTest(unittest.TestCase):
    """The funds bound is per CoinJoin; remixing makes the sum exceed the funding."""

    @staticmethod
    def _coinjoins(*input_sats):
        return {
            f"tx{index}": {"total_input_sats": value, "inputs": [], "outputs": []}
            for index, value in enumerate(input_sats)
        }

    def test_remixed_rounds_may_exceed_total_funding(self):
        checks = build_scenario_checks(
            {"wallet_count": 2, "total_initial_funds_sats": 1000},
            self._coinjoins(800, 700),
        )

        self.assertEqual(checks["coinjoin_analysis_input_sats"], 1500)
        self.assertEqual(checks["max_coinjoin_input_sats"], 800)
        self.assertTrue(checks["input_sats_within_scenario_funds"])
        self.assertEqual(checks["coinjoin_input_sats_to_funds_ratio"], 1.5)

    def test_single_coinjoin_above_funding_fails(self):
        checks = build_scenario_checks(
            {"wallet_count": 2, "total_initial_funds_sats": 1000},
            self._coinjoins(400, 1200),
        )

        self.assertEqual(checks["max_coinjoin_input_sats"], 1200)
        self.assertFalse(checks["input_sats_within_scenario_funds"])

    def test_joinmarket_accepts_a_subset_of_the_scenario_wallets(self):
        """A taker picks a few counterparties per round; unchosen makers never appear."""
        coinjoins = {
            "tx0": {
                "total_input_sats": 100,
                "inputs": [
                    {"wallet_name": "wallet-000"},
                    {"wallet_name": "wallet-001"},
                ],
                "outputs": [],
            }
        }
        checks = build_scenario_checks(
            {"wallet_count": 10, "total_initial_funds_sats": 1000},
            coinjoins,
            "joinmarket",
        )

        self.assertEqual(checks["wallet_count_rule"], "subset")
        self.assertEqual(checks["coinjoin_analysis_wallet_count"], 2)
        self.assertTrue(checks["wallet_count_matches"])

    def test_wasabi_still_requires_every_wallet(self):
        coinjoins = {
            "tx0": {
                "total_input_sats": 100,
                "inputs": [{"wallet_name": "wallet-000"}],
                "outputs": [],
            }
        }
        checks = build_scenario_checks({"wallet_count": 10, "total_initial_funds_sats": 1000}, coinjoins, "wasabi2")

        self.assertEqual(checks["wallet_count_rule"], "exact")
        self.assertFalse(checks["wallet_count_matches"])

    def test_joinmarket_without_any_observed_wallet_fails(self):
        checks = build_scenario_checks(
            {"wallet_count": 10, "total_initial_funds_sats": 1000},
            self._coinjoins(100),
            "joinmarket",
        )

        self.assertFalse(checks["wallet_count_matches"])

    def test_without_scenario_funds_the_check_is_undefined(self):
        checks = build_scenario_checks(None, self._coinjoins(400))

        self.assertIsNone(checks["input_sats_within_scenario_funds"])
        self.assertIsNone(checks["coinjoin_input_sats_to_funds_ratio"])


if __name__ == "__main__":
    unittest.main()
