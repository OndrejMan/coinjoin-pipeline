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
        heuristics=types.SimpleNamespace(set_test_values_enabled=lambda *_args, **_kwargs: None),
    )
else:
    _ = _blocksci

from exporters import unified_report
from exporters.heuristics import WASABI2_BLOCKSCI_DENOMINATIONS
from exporters.markdown_report import render_report
from exporters.unified_report import (
    SCHEMA_VERSION,
    build_emulator_data,
    build_integration_diagnostics,
    build_report,
    build_run_manifest,
    compare_run_manifests,
    evaluate_cluster_assignments,
    explain_joinmarket_definite_heuristic,
    explain_joinmarket_possible_heuristic,
    explain_wasabi2_heuristic,
    export_blocksci_records,
    export_blocksci_cluster_assignments,
    exported_block_targets,
    fill_missing_block_heights,
    filter_coinjoin_analysis_false_positives,
    load_exported_block_tx_index,
    load_false_positive_txids,
    load_scenario,
    load_wasabi_round_labels,
    normalize_coinjoin_analysis,
    normalize_scenario,
    parse_args,
    parse_min_input_count,
    save_json,
    sha256_json,
)


def coinjoin_analysis_fixture():
    return {
        "coinjoins": {
            "txA": {
                "txid": "txA",
                "broadcast_time": "2026-01-01 00:00:00.000",
                "inputs": {
                    "0": {"value": 150000, "address": "input-a", "wallet_name": "wallet-000"},
                },
                "outputs": {
                    "0": {"value": 100000, "address": "output-a0", "wallet_name": "wallet-000"},
                    "1": {"value": 50000, "address": "output-a1", "wallet_name": "wallet-000"},
                    "2": {"value": 50000, "address": "output-a2", "wallet_name": "wallet-000"},
                },
            }
        }
    }


def coinjoin_analysis_fixture_with_block_height(height):
    fixture = coinjoin_analysis_fixture()
    fixture["coinjoins"]["txA"]["block_height"] = height
    return fixture


def wasabi2_passing_coinjoin_fixture():
    return {
        "coinjoins": {
            "txA": {
                "txid": "txA",
                "block_height": 226,
                "inputs": {
                    "0": {"value": 5000, "address": "input-a0", "wallet_name": "wallet-000"},
                    "1": {"value": 4000, "address": "input-a1", "wallet_name": "wallet-001"},
                    "2": {"value": 3000, "address": "input-a2", "wallet_name": "wallet-002"},
                    "3": {"value": 2000, "address": "input-a3", "wallet_name": "wallet-003"},
                    "4": {"value": 1000, "address": "input-a4", "wallet_name": "wallet-004"},
                },
                "outputs": {
                    "0": {"value": 5000, "address": "output-a0", "wallet_name": "wallet-000"},
                    "1": {"value": 5000, "address": "output-a1", "wallet_name": "wallet-001"},
                    "2": {"value": 5000, "address": "output-a2", "wallet_name": "wallet-002"},
                    "3": {"value": 5000, "address": "output-a3", "wallet_name": "wallet-003"},
                    "4": {"value": 5000, "address": "output-a4", "wallet_name": "wallet-004"},
                },
            }
        }
    }


def joinmarket_passing_coinjoin_fixture():
    return {
        "coinjoins": {
            "txA": {
                "txid": "txA",
                "block_height": 42,
                "inputs": {
                    "0": {"value": 195002, "address": "input-a0", "wallet_name": "wallet-000"},
                    "1": {"value": 455004, "address": "input-a1", "wallet_name": "wallet-001"},
                    "2": {"value": 46682, "address": "input-a2", "wallet_name": "wallet-002"},
                    "3": {"value": 2955004, "address": "input-a3", "wallet_name": "wallet-003"},
                    "4": {"value": 2915006, "address": "input-a4", "wallet_name": "wallet-004"},
                },
                "outputs": {
                    "0": {"value": 40000, "address": "mix-a0", "wallet_name": "wallet-000"},
                    "1": {"value": 40000, "address": "mix-a1", "wallet_name": "wallet-001"},
                    "2": {"value": 40000, "address": "mix-a2", "wallet_name": "wallet-002"},
                    "3": {"value": 40000, "address": "mix-a3", "wallet_name": "wallet-003"},
                    "4": {"value": 40000, "address": "mix-a4", "wallet_name": "wallet-004"},
                    "5": {"value": 160002, "address": "change-a0", "wallet_name": "wallet-000"},
                    "6": {"value": 420004, "address": "change-a1", "wallet_name": "wallet-001"},
                    "7": {"value": 11682, "address": "change-a2", "wallet_name": "wallet-002"},
                    "8": {"value": 2920004, "address": "change-a3", "wallet_name": "wallet-003"},
                    "9": {"value": 2880006, "address": "change-a4", "wallet_name": "wallet-004"},
                },
            }
        }
    }


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
            "sources": sources,
        },
    )


class FakeTx:
    def __init__(self, txid, block_height):
        self.hash = txid
        self.block_height = block_height


class FakeBlockchain:
    def __init__(self, _config, tx_heights=None, block_count=0):
        self.tx_heights = tx_heights or {}
        self.block_count = block_count

    def __len__(self):
        return self.block_count

    def tx_with_hash(self, txid):
        if txid not in self.tx_heights:
            raise KeyError(txid)
        return FakeTx(txid, self.tx_heights[txid])


class FakeJoinMarketHeuristics:
    def __init__(self, results):
        self.results = results

    def is_definite_coinjoin(self, _min_base_fee, _percentage_fee, _max_depth):
        return lambda tx: self.results.get(str(tx.hash), 1)

    def is_possible_coinjoin(self, _min_base_fee, _percentage_fee, _max_depth):
        return lambda tx: self.results.get(str(tx.hash), 1)


class FakeBlockSciModule:
    def __init__(self, tx_heights, block_count, detector_results=None):
        self.tx_heights = tx_heights
        self.block_count = block_count
        self.heuristics = types.SimpleNamespace(
            coinjoin=FakeJoinMarketHeuristics(detector_results or {})
        )

    def Blockchain(self, config):
        return FakeBlockchain(config, self.tx_heights, self.block_count)


def complete_image_ids():
    return {
        "blocksci": "sha256:blocksci-id",
        "coinjoin_analysis": "sha256:analysis-id",
        "coinjoin_emulator": "sha256:emulator-id",
        "wrapper": "sha256:wrapper-id",
    }


def complete_image_digests():
    return {
        "blocksci": "ghcr.io/ondrejman/blocksci-complete@sha256:blocksci",
        "coinjoin_analysis": "ghcr.io/ondrejman/coinjoin-analysis@sha256:analysis",
        "coinjoin_emulator": "ghcr.io/ondrejman/coinjoin-emulator@sha256:emulator",
        "wrapper": "ghcr.io/ondrejman/coinjoin-pipeline@sha256:wrapper",
    }


def complete_image_refs():
    return {
        "blocksci": "blocksci:test",
        "coinjoin_analysis": "coinjoin-analysis:test",
        "coinjoin_emulator": "coinjoin-emulator:test",
        "wrapper": "wrapper:test",
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
            records, skipped = export_blocksci_records(
                Path("/tmp/config.json"), "wasabi2", None
            )

        self.assertEqual(records, {})
        self.assertEqual(skipped, [])
        self.assertEqual(chain.calls, [(0, 10, "wasabi2")])

    def test_external_report_marks_metrics_as_baseline_agreement_only(self):
        report = build_report(
            Path("/tmp/external-run"),
            normalize_coinjoin_analysis(coinjoin_analysis_fixture()),
            blocksci_fixture(),
            "wasabi2",
            mode="external",
            network="bitcoin",
        )

        self.assertEqual(report["run"]["mode"], "external")
        self.assertEqual(report["run"]["network"], "bitcoin")
        self.assertEqual(report["evaluation_scope"], "baseline_agreement_only")
        self.assertIsNone(report["detection_confusion_matrix"])
        markdown = render_report(report)
        self.assertIn("precision, recall, and F1 are intentionally unavailable", markdown)

    def test_run_manifest_records_reproduction_command(self):
        with mock.patch.dict(os.environ, {"REPRODUCTION_COMMAND": "./runIt.sh full-run --engine joinmarket"}):
            manifest = build_run_manifest(
                Path("/tmp/run"), None, "joinmarket", "joinmarket", 1, False, 0,
                "definite", 5000, 0.00004, 200000,
            )
        self.assertEqual(manifest["execution"]["reproduction_command"], "./runIt.sh full-run --engine joinmarket")

    def test_parse_args_accepts_test_values(self):
        args = parse_args(["--test-values"])

        self.assertTrue(args.test_values)

    def test_min_input_count_requires_a_positive_integer_or_default(self):
        self.assertIsNone(parse_min_input_count("default"))
        self.assertEqual(parse_min_input_count("7"), 7)
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(ArgumentTypeError):
                parse_min_input_count(value)

    def test_parse_args_reports_invalid_min_input_count_as_a_cli_error(self):
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                parse_args(["--min-input-count", value])

    def test_parse_args_accepts_joinmarket_detector_settings(self):
        args = parse_args([
            "--engine",
            "joinmarket",
            "--coinjoin-type",
            "joinmarket",
            "--joinmarket-detector",
            "possible",
            "--joinmarket-min-base-fee",
            "7000",
            "--joinmarket-percentage-fee",
            "0.001",
            "--joinmarket-max-depth",
            "100",
        ])

        self.assertEqual(args.engine, "joinmarket")
        self.assertEqual(args.joinmarket_detector, "possible")
        self.assertEqual(args.joinmarket_min_base_fee, 7000)
        self.assertEqual(args.joinmarket_percentage_fee, 0.001)
        self.assertEqual(args.joinmarket_max_depth, 100)

    def test_parse_args_accepts_manifest_provenance(self):
        args = parse_args([
            "--blocksci-image",
            "blocksci:test",
            "--coinjoin-analysis-image",
            "coinjoin-analysis:test",
            "--coinjoin-emulator-image",
            "coinjoin-emulator:test",
            "--wrapper-image",
            "wrapper:test",
            "--emulator-git-commit",
            "abc123",
        ])

        self.assertEqual(args.blocksci_image, "blocksci:test")
        self.assertEqual(args.coinjoin_analysis_image, "coinjoin-analysis:test")
        self.assertEqual(args.coinjoin_emulator_image, "coinjoin-emulator:test")
        self.assertEqual(args.wrapper_image, "wrapper:test")
        self.assertEqual(args.emulator_git_commit, "abc123")

    def test_run_manifest_comparison_reports_changed_provenance(self):
        previous = build_run_manifest(
            Path("/tmp/run"),
            {"name": "scenario-a", "sha256": "old-hash"},
            "joinmarket",
            "joinmarket",
            1,
            True,
            0,
            "definite",
            5000,
            0.00004,
            200000,
            blocksci_image="blocksci:old",
            coinjoin_analysis_image="coinjoin-analysis:old",
            coinjoin_emulator_image="coinjoin-emulator:old",
            wrapper_image="wrapper:old",
            emulator_git_commit="old-commit",
        )
        current = build_run_manifest(
            Path("/tmp/run"),
            {"name": "scenario-a", "sha256": "new-hash"},
            "joinmarket",
            "joinmarket",
            1,
            True,
            0,
            "possible",
            5000,
            0.00004,
            200000,
            blocksci_image="blocksci:new",
            coinjoin_analysis_image="coinjoin-analysis:old",
            coinjoin_emulator_image="coinjoin-emulator:old",
            wrapper_image="wrapper:old",
            emulator_git_commit="old-commit",
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
                "wasabi2",
                "wasabi",
                1,
                False,
                0,
                "definite",
                5000,
                0.00004,
                200000,
            )
            report = build_report(
                run_dir,
                coinjoin_analysis,
                blocksci_fixture(),
                "wasabi2",
                {"name": "default", "sha256": "new-hash"},
                min_input_count=1,
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
            scenario = load_scenario(run_dir, None)
            report = build_report(
                run_dir,
                coinjoin_analysis,
                blocksci_fixture(),
                "wasabi2",
                scenario,
                emulator_data=emulator_data_fixture(),
            )

        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["run"]["id"], "2026-05-24_15-42_default")
        self.assertEqual(report["run"]["started_at"], "2026-05-24T15:42:00")
        self.assertEqual(report["run"]["scenario_name"], "default")
        self.assertEqual(report["run"]["scenario_sha256"], sha256_json(scenario_fixture()))
        self.assertEqual(report["run_manifest"]["scenario"]["sha256"], sha256_json(scenario_fixture()))
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
                "wallet_count_matches": False,
                "coinjoin_analysis_input_sats": 150000,
                "scenario_initial_funds_sats": 3250000,
                "input_sats_within_scenario_funds": True,
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
            report["detection_confusion_matrix"],
            {
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
            },
        )

    def test_build_report_includes_image_digests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            report = build_report(
                run_dir,
                normalize_coinjoin_analysis(coinjoin_analysis_fixture()),
                {},
                "joinmarket",
                blocksci_image_digest="sha256:blocksci",
                coinjoin_analysis_image_digest="sha256:analysis",
                coinjoin_emulator_image_digest="sha256:emulator",
                wrapper_image_digest="sha256:wrapper",
            )

        self.assertEqual(report["run_manifest"]["image_digests"]["blocksci"], "sha256:blocksci")
        self.assertEqual(report["run_manifest"]["image_digests"]["coinjoin_analysis"], "sha256:analysis")
        self.assertEqual(report["run_manifest"]["image_digests"]["coinjoin_emulator"], "sha256:emulator")
        self.assertEqual(report["run_manifest"]["image_digests"]["wrapper"], "sha256:wrapper")

    def test_build_report_includes_integration_diagnostics(self):
        diagnostics = {"status": "ok", "problems": [], "images": {}, "chain": {}, "target_txids": {}, "detector": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            report = build_report(
                run_dir,
                normalize_coinjoin_analysis(coinjoin_analysis_fixture()),
                blocksci_fixture(),
                "wasabi2",
                integration_diagnostics=diagnostics,
            )

        self.assertEqual(report["integration_diagnostics"], diagnostics)

    def test_build_emulator_data_labels_chain_transactions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            txid = "a" * 64
            coordinator_dir = (
                run_dir / "coinjoin_emulator_data" / "data" / "wasabi-coordinator"
            )
            coordinator_dir.mkdir(parents=True)
            (coordinator_dir / "Logs.txt").write_text(
                "2026-01-01 00:00:00 [INFO] Round (abc): "
                f"Successfully broadcast the coinjoin: {txid}.\n",
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
                            {"txid": "coinbase", "vin": [{"coinbase": "00"}], "vout": []},
                            {
                                "txid": "funding",
                                "vin": [{"txid": "coinbase", "vout": 0}],
                                "vout": [
                                    {
                                        "value": 0.0015,
                                        "n": 0,
                                        "scriptPubKey": {"address": "input-a", "type": "witness_v0_keyhash"},
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
                                        "scriptPubKey": {"address": "output-a0", "type": "witness_v0_keyhash"},
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
        self.assertEqual(emulator_data["transactions"][txid]["inputs"][0]["wallet_name"], "wallet-000")
        self.assertEqual(emulator_data["transactions"][txid]["outputs"][0]["wallet_name"], "wallet-000")
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

    def test_build_emulator_data_leaves_labels_unknown_without_producer_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            save_json(
                block_dir / "block_1.json",
                {
                    "height": 1,
                    "tx": [{"txid": "txA", "vin": [{"txid": "funding", "vout": 0}], "vout": []}],
                },
            )

            emulator_data = build_emulator_data(
                run_dir, coinjoin_analysis_fixture(), "wasabi2"
            )
            report = build_report(
                run_dir,
                normalize_coinjoin_analysis(coinjoin_analysis_fixture()),
                blocksci_fixture(),
                "wasabi2",
                emulator_data=emulator_data,
            )

        self.assertIsNone(emulator_data["transactions"]["txA"]["is_coinjoin"])
        self.assertEqual(emulator_data["summary"]["unknown_transactions"], 1)
        self.assertFalse(emulator_data["label_provenance"]["independent"])
        self.assertIsNone(report["detection_confusion_matrix"])
        self.assertEqual(report["evaluation_scope"], "emulator_labels_unavailable")
        self.assertIn("Independent emulator producer labels were unavailable", render_report(report))

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
                    "tx": [{"txid": "txA", "vin": [{"txid": "funding", "vout": 0}], "vout": []}],
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
                            "vin": [
                                {"txid": f"funding-{index}", "vout": 0}
                                for index in range(5)
                            ],
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
        self.assertIn("no parseable broadcast records", emulator_data["label_provenance"]["unavailable_reason"])
        self.assertIsNone(emulator_data["transactions"]["candidate"]["is_coinjoin"])
        self.assertEqual(emulator_data["summary"]["wasabi_parseability_candidate_txids"], ["candidate"])

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
                "2026-01-01 00:00:00 [INFO] Round (abc): "
                f"Successfully broadcast the coinjoin: {missing_txid}.\n",
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
            report = build_report(run_dir, {}, {}, "wasabi2", emulator_data=emulator_data)

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
                    "tx": [{"txid": "txA", "vin": [{"txid": "funding", "vout": 0}], "vout": []}],
                },
            )

            emulator_data = build_emulator_data(
                run_dir,
                coinjoin_analysis_fixture(),
                "wasabi2",
            )

        self.assertFalse(emulator_data["label_provenance"]["independent"])
        self.assertIn("does not match manifest", emulator_data["label_provenance"]["unavailable_reason"])
        self.assertIsNone(emulator_data["transactions"]["txA"]["is_coinjoin"])

    def test_exported_block_targets_ignore_coinbase_and_capture_max_height(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            save_json(
                block_dir / "block_0.json",
                {
                    "height": 0,
                    "tx": [
                        {"txid": "coinbase", "vin": [{"coinbase": "00"}]},
                        {"txid": "funding", "vin": [{"txid": "coinbase", "vout": 0}]},
                    ],
                },
            )
            save_json(
                block_dir / "block_2.json",
                {
                    "height": 2,
                    "tx": [
                        {"txid": "txA", "vin": [{"txid": "funding", "vout": 0}]},
                    ],
                },
            )

            targets, summary = exported_block_targets(run_dir)

        self.assertEqual([target["txid"] for target in targets], ["funding", "txA"])
        self.assertEqual(summary["exported_block_count"], 2)
        self.assertEqual(summary["max_exported_block_height"], 2)

    def test_integration_diagnostics_ok_when_provenance_chain_and_detector_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            save_json(
                block_dir / "block_0.json",
                {
                    "height": 0,
                    "tx": [
                        {"txid": "coinbase", "vin": [{"coinbase": "00"}]},
                        {"txid": "funding", "vin": [{"txid": "coinbase", "vout": 0}]},
                    ],
                },
            )
            save_json(
                block_dir / "block_1.json",
                {
                    "height": 1,
                    "tx": [
                        {"txid": "txA", "vin": [{"txid": "funding", "vout": 0}]},
                    ],
                },
            )
            diagnostics = build_integration_diagnostics(
                run_dir,
                Path("/tmp/config.json"),
                FakeBlockSciModule({"funding": 0, "txA": 1}, 2, {"funding": 1, "txA": 0}),
                {"txA": blocksci_fixture()["txA"]},
                "joinmarket",
                complete_image_refs(),
                image_ids=complete_image_ids(),
                image_digests=complete_image_digests(),
            )

        self.assertEqual(diagnostics["status"], "ok")
        self.assertEqual(diagnostics["chain"]["blocksci_chain_height"], 1)
        self.assertEqual(diagnostics["chain"]["max_exported_block_height"], 1)
        self.assertEqual(diagnostics["target_txids"]["total"], 2)
        self.assertEqual(diagnostics["target_txids"]["present"], 2)
        self.assertEqual(diagnostics["detector"]["checked"], 2)
        self.assertEqual(diagnostics["detector"]["disagreements"], 0)

    def test_integration_diagnostics_not_ok_when_image_digest_missing(self):
        digests = complete_image_digests()
        digests["blocksci"] = None
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            (run_dir / "coinjoin_emulator_data" / "data" / "btc-node").mkdir(parents=True)
            save_json(
                run_dir / "coinjoin_emulator_data" / "data" / "btc-node" / "block_0.json",
                {"height": 0, "tx": []},
            )

            diagnostics = build_integration_diagnostics(
                run_dir,
                Path("/tmp/config.json"),
                FakeBlockSciModule({}, 1),
                {},
                "wasabi2",
                complete_image_refs(),
                image_ids=complete_image_ids(),
                image_digests=digests,
            )

        self.assertEqual(diagnostics["status"], "not_ok")
        self.assertEqual(diagnostics["images"]["blocksci"]["status"], "not_ok")
        self.assertIn("blocksci image provenance is incomplete", diagnostics["problems"][0])

    def test_integration_diagnostics_not_ok_when_chain_height_differs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            save_json(block_dir / "block_2.json", {"height": 2, "tx": []})

            diagnostics = build_integration_diagnostics(
                run_dir,
                Path("/tmp/config.json"),
                FakeBlockSciModule({}, 2),
                {},
                "wasabi2",
                complete_image_refs(),
                image_ids=complete_image_ids(),
                image_digests=complete_image_digests(),
            )

        self.assertEqual(diagnostics["status"], "not_ok")
        self.assertEqual(diagnostics["chain"]["blocksci_chain_height"], 1)
        self.assertEqual(diagnostics["chain"]["max_exported_block_height"], 2)

    def test_integration_diagnostics_not_ok_when_target_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            save_json(
                block_dir / "block_0.json",
                {
                    "height": 0,
                    "tx": [
                        {"txid": "txA", "vin": [{"txid": "funding", "vout": 0}]},
                    ],
                },
            )

            diagnostics = build_integration_diagnostics(
                run_dir,
                Path("/tmp/config.json"),
                FakeBlockSciModule({}, 1),
                {},
                "wasabi2",
                complete_image_refs(),
                image_ids=complete_image_ids(),
                image_digests=complete_image_digests(),
            )

        self.assertEqual(diagnostics["status"], "not_ok")
        self.assertEqual(diagnostics["target_txids"]["missing"], 1)
        self.assertIn("missing from BlockSci", " ".join(diagnostics["problems"]))

    def test_integration_diagnostics_not_ok_when_joinmarket_direct_detector_disagrees(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            save_json(
                block_dir / "block_0.json",
                {
                    "height": 0,
                    "tx": [
                        {"txid": "txA", "vin": [{"txid": "funding", "vout": 0}]},
                    ],
                },
            )

            diagnostics = build_integration_diagnostics(
                run_dir,
                Path("/tmp/config.json"),
                FakeBlockSciModule({"txA": 0}, 1, {"txA": 1}),
                {"txA": blocksci_fixture()["txA"]},
                "joinmarket",
                complete_image_refs(),
                image_ids=complete_image_ids(),
                image_digests=complete_image_digests(),
            )

        self.assertEqual(diagnostics["status"], "not_ok")
        self.assertEqual(diagnostics["detector"]["disagreements"], 1)

    def test_build_emulator_data_adds_joinmarket_round_labels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            block_dir = run_dir / "coinjoin_emulator_data" / "data" / "btc-node"
            block_dir.mkdir(parents=True)
            (run_dir / "coinjoin_emulator_data" / "data" / "joinmarket_round_events.json").write_text(
                json.dumps([
                    {
                        "round_id": 1,
                        "status": "confirmed",
                        "taker": "jcs-000",
                        "candidate_makers": ["jcs-001", "jcs-002"],
                        "destination_address": "output-a0",
                        "txid": "txA",
                    },
                    {
                        "round_id": 2,
                        "status": "failed",
                        "destination_address": "failed-destination",
                        "txid": "txB",
                    },
                ]),
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
                                {"n": 0, "value": 0.0015, "scriptPubKey": {"address": "input-a"}},
                            ],
                        },
                        {
                            "txid": "txA",
                            "vin": [{"txid": "funding", "vout": 0}],
                            "vout": [
                                {"n": 0, "value": 0.001, "scriptPubKey": {"address": "output-a0"}},
                                {"n": 1, "value": 0.0005, "scriptPubKey": {"address": "output-a1"}},
                            ],
                        },
                        {
                            "txid": "txB",
                            "vin": [{"txid": "funding", "vout": 0}],
                            "vout": [
                                {"n": 0, "value": 0.001, "scriptPubKey": {"address": "failed-destination"}},
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

    def test_build_emulator_data_rejects_malformed_joinmarket_label_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            label_path = (
                run_dir / "coinjoin_emulator_data" / "data" / "joinmarket_round_events.json"
            )
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
                "wasabi2",
                emulator_data=emulator_data_fixture(),
            )

        matrix = report["detection_confusion_matrix"]
        self.assertEqual(matrix["true_positives"], 0)
        self.assertEqual(matrix["false_positives"], 1)
        self.assertEqual(matrix["false_negatives"], 1)
        self.assertEqual(matrix["true_negatives"], 1)
        self.assertEqual(matrix["precision"], 0.0)
        self.assertEqual(matrix["recall"], 0.0)
        self.assertEqual(matrix["false_positive_txids"], ["txB"])
        self.assertEqual(matrix["false_negative_txids"], ["txA"])

    def test_report_warns_when_production_thresholds_detect_no_regtest_wasabi(self):
        emulator_data = emulator_data_fixture()
        for transaction in emulator_data["transactions"].values():
            transaction["block_height"] = 100
        report = build_report(
            Path("/tmp/run"),
            {},
            {},
            "wasabi2",
            emulator_data=emulator_data,
        )

        self.assertEqual(
            report["warnings"][0]["code"],
            "wasabi_production_threshold_zero_detections",
        )
        self.assertIn("production minimum-input threshold", render_report(report))

    def test_clustering_unavailable_reason_is_reported(self):
        evaluation = evaluate_cluster_assignments(
            emulator_data_fixture(),
            unavailable_reason="cluster export failed",
        )

        self.assertFalse(evaluation["available"])
        self.assertEqual(evaluation["reason"], "cluster export failed")
        self.assertEqual(evaluation["labeled_addresses"], 0)

    def test_build_report_includes_cluster_export_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            report = build_report(
                run_dir,
                coinjoin_analysis,
                blocksci_fixture(),
                "wasabi2",
                emulator_data=emulator_data_fixture(),
                cluster_export_error="cluster export failed",
            )

        self.assertFalse(report["clustering_evaluation"]["available"])
        self.assertEqual(report["clustering_evaluation"]["reason"], "cluster export failed")

    def test_export_blocksci_cluster_assignments_skips_empty_clusters(self):
        class FakeHeuristic:
            def __and__(self, _other):
                return self

        class FakeCoinjoinHeuristics:
            one_output_consolidation_2hops = FakeHeuristic()
            two_equal_output_consolidation_1hop = FakeHeuristic()

        class FakeCluster:
            def __init__(self, index, count):
                self.index = index
                self.count = count

            def address_count(self):
                return self.count

        class FakeClusterer:
            def cluster_with_address(self, address):
                if address == "known-a":
                    return FakeCluster(7, 2)
                return FakeCluster(0, 0)

        class FakeCoinjoinClusterManager:
            @staticmethod
            def create_clustering(**_kwargs):
                return FakeClusterer()

        class FakeClusterBlockchain:
            def __init__(self, _config):
                pass

            def address_from_string(self, address):
                return address

        previous_blocksci = unified_report.blocksci
        unified_report.blocksci = types.SimpleNamespace(
            Blockchain=FakeClusterBlockchain,
            heuristics=types.SimpleNamespace(coinjoin=FakeCoinjoinHeuristics()),
            cluster=types.SimpleNamespace(CoinjoinClusterManager=FakeCoinjoinClusterManager),
        )
        try:
            predicted, error = export_blocksci_cluster_assignments(
                Path("/tmp/config.json"),
                {
                    "transactions": {
                        "tx": {
                            "inputs": [{"address": "known-a", "wallet_name": "wallet-a"}],
                            "outputs": [{"address": "unknown-a", "wallet_name": "wallet-b"}],
                        }
                    }
                },
                "wasabi2",
                Path("/tmp/clusters"),
            )
        finally:
            unified_report.blocksci = previous_blocksci

        self.assertIsNone(error)
        self.assertEqual(predicted, {"known-a": "7"})

    def test_export_blocksci_cluster_assignments_creates_output_parent(self):
        class FakeHeuristic:
            def __and__(self, _other):
                return self

        class FakeCoinjoinHeuristics:
            one_output_consolidation_2hops = FakeHeuristic()
            two_equal_output_consolidation_1hop = FakeHeuristic()

        class FakeCluster:
            index = 3

            def address_count(self):
                return 1

        class FakeClusterer:
            def cluster_with_address(self, _address):
                return FakeCluster()

        class FakeCoinjoinClusterManager:
            @staticmethod
            def create_clustering(**kwargs):
                output_path = Path(kwargs["output_path"])
                if not output_path.parent.is_dir():
                    raise RuntimeError("missing clustering parent")
                return FakeClusterer()

        class FakeClusterBlockchain:
            def __init__(self, _config):
                pass

            def address_from_string(self, address):
                return address

        previous_blocksci = unified_report.blocksci
        unified_report.blocksci = types.SimpleNamespace(
            Blockchain=FakeClusterBlockchain,
            heuristics=types.SimpleNamespace(coinjoin=FakeCoinjoinHeuristics()),
            cluster=types.SimpleNamespace(CoinjoinClusterManager=FakeCoinjoinClusterManager),
        )
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                output_dir = Path(tmpdir) / "missing" / "joinmarket_emulator_report"
                predicted, error = export_blocksci_cluster_assignments(
                    Path("/tmp/config.json"),
                    {
                        "transactions": {
                            "tx": {
                                "inputs": [{"address": "known-a", "wallet_name": "wallet-a"}],
                                "outputs": [],
                            }
                        }
                    },
                    "joinmarket",
                    output_dir,
                )
        finally:
            unified_report.blocksci = previous_blocksci

        self.assertIsNone(error)
        self.assertEqual(predicted, {"known-a": "3"})

    def test_evaluate_cluster_assignments_detects_overmerge_and_undermerge(self):
        emulator_data = {
            "transactions": {
                "txA": {
                    "inputs": [
                        {"address": "a1", "wallet_name": "wallet-a"},
                        {"address": "b1", "wallet_name": "wallet-b"},
                    ],
                    "outputs": [
                        {"address": "a2", "wallet_name": "wallet-a"},
                        {"address": "b2", "wallet_name": "wallet-b"},
                    ],
                }
            }
        }
        predicted = {"a1": "cluster-1", "a2": "cluster-2", "b1": "cluster-1", "b2": "cluster-3"}

        evaluation = evaluate_cluster_assignments(emulator_data, predicted)

        self.assertTrue(evaluation["available"])
        self.assertEqual(evaluation["overmerged_clusters"], 1)
        self.assertEqual(evaluation["undermerged_wallets"], 2)
        self.assertEqual(evaluation["pairwise_false_positives"], 1)
        self.assertEqual(evaluation["pairwise_false_negatives"], 2)

    def test_build_report_blocksci_only_and_missed_by_blocksci(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), "wasabi2")

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
        self.assertEqual(report["divergences"]["missed_by_blocksci"][0]["coinjoin_analysis"]["wallets"], ["wallet-000"])
        self.assertEqual(report["divergences"]["blocksci_only"][0]["txid"], "txB")
        self.assertIsNone(report["divergences"]["blocksci_only"][0]["coinjoin_analysis"])
        explanation = report["transactions"]["txA"]["coinjoin_analysis"]["blocksci_heuristic_explanation"]
        self.assertEqual(explanation["heuristic"], "wasabi2")
        self.assertFalse(explanation["would_pass_python_rules"])
        self.assertIn("unique_input_addresses", explanation["failed_rules"])

    def test_wasabi2_default_threshold_fails_small_current_missed_shape(self):
        records = normalize_coinjoin_analysis(coinjoin_analysis_fixture_with_block_height(226))
        explanation = explain_wasabi2_heuristic(records["txA"], min_input_count=None, test_values=True)

        input_count_rule = next(rule for rule in explanation["rules"] if rule["name"] == "input_count")
        self.assertFalse(input_count_rule["passed"])
        self.assertIn("input_count", explanation["failed_rules"])

    def test_wasabi2_low_min_input_count_can_pass_input_count_rule(self):
        records = normalize_coinjoin_analysis(coinjoin_analysis_fixture_with_block_height(226))
        explanation = explain_wasabi2_heuristic(records["txA"], min_input_count=1, test_values=True)

        input_count_rule = next(rule for rule in explanation["rules"] if rule["name"] == "input_count")
        self.assertTrue(input_count_rule["passed"])
        self.assertNotIn("input_count", explanation["failed_rules"])

    def test_wasabi2_mirror_includes_powers_of_ten_times_five_denominations(self):
        record = {
            "txid": "txA",
            "block_height": 226,
            "inputs": [
                {
                    "value": 10_000 - index,
                    "address": f"input-{index}",
                    "address_type": "WITNESS_PUBKEYHASH",
                }
                for index in range(5)
            ],
            "outputs": [
                {
                    "value": value,
                    "address": f"output-{index}",
                    "address_type": "WITNESS_PUBKEYHASH",
                    "is_standard_denom": True,
                }
                for index, value in enumerate(([5_000_000] * 7) + ([20_000] * 47) + ([17_781] * 7))
            ],
        }

        explanation = explain_wasabi2_heuristic(record, min_input_count=1, test_values=True)

        denom_rule = next(rule for rule in explanation["rules"] if rule["name"] == "wasabi2_denominations")
        self.assertIn(5_000_000, WASABI2_BLOCKSCI_DENOMINATIONS)
        self.assertTrue(denom_rule["passed"])
        self.assertEqual(denom_rule["observed"], "54/61 outputs")
        self.assertNotIn("wasabi2_denominations", explanation["failed_rules"])

    def test_wasabi2_non_descending_values_fail_matching_rules(self):
        records = normalize_coinjoin_analysis(wasabi2_passing_coinjoin_fixture())
        record = records["txA"]
        record["inputs"][1]["value"] = 6000
        record["outputs"][1]["value"] = 2000

        explanation = explain_wasabi2_heuristic(record, min_input_count=1, test_values=True)

        self.assertIn("input_values_descending", explanation["failed_rules"])
        self.assertIn("output_values_descending", explanation["failed_rules"])

    def test_wasabi2_fewer_than_five_unique_addresses_fail(self):
        records = normalize_coinjoin_analysis(coinjoin_analysis_fixture_with_block_height(226))
        explanation = explain_wasabi2_heuristic(records["txA"], min_input_count=1, test_values=True)

        self.assertIn("unique_input_addresses", explanation["failed_rules"])
        self.assertIn("unique_output_addresses", explanation["failed_rules"])

    def test_joinmarket_definite_explanation_records_subset_conditions(self):
        records = normalize_coinjoin_analysis(joinmarket_passing_coinjoin_fixture())
        explanation = explain_joinmarket_definite_heuristic(records["txA"])

        self.assertEqual(explanation["heuristic"], "joinmarket_definite")
        self.assertTrue(explanation["would_pass_python_rules"])
        self.assertEqual(explanation["failed_rules"], [])
        subset_rule = next(rule for rule in explanation["rules"] if rule["name"] == "subset_partition_after_fee")
        self.assertTrue(subset_rule["passed"])
        self.assertEqual(
            subset_rule["observed"]["bucket_goals_after_fee"],
            [195002, 455004, 46682, 2955004, 2915006],
        )

    def test_joinmarket_possible_explanation_records_subset_conditions(self):
        records = normalize_coinjoin_analysis(joinmarket_passing_coinjoin_fixture())
        explanation = explain_joinmarket_possible_heuristic(records["txA"])

        self.assertEqual(explanation["heuristic"], "joinmarket_possible")
        self.assertTrue(explanation["would_pass_python_rules"])
        subset_rule = next(rule for rule in explanation["rules"] if rule["name"] == "two_bucket_subset_after_fee")
        self.assertTrue(subset_rule["passed"])
        self.assertEqual(subset_rule["observed"]["bucket_goals_after_fee"], [35000, 35000])

    def test_build_report_adds_joinmarket_explanation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            coinjoin_analysis = normalize_coinjoin_analysis(joinmarket_passing_coinjoin_fixture())
            report = build_report(run_dir, coinjoin_analysis, {}, "joinmarket")

        explanation = report["transactions"]["txA"]["coinjoin_analysis"]["blocksci_heuristic_explanation"]
        self.assertEqual(explanation["heuristic"], "joinmarket_definite")
        self.assertIn("subset_partition_after_fee", [rule["name"] for rule in explanation["rules"]])

    def test_build_report_adds_possible_joinmarket_explanation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            coinjoin_analysis = normalize_coinjoin_analysis(joinmarket_passing_coinjoin_fixture())
            report = build_report(run_dir, coinjoin_analysis, {}, "joinmarket", joinmarket_detector="possible")

        explanation = report["transactions"]["txA"]["coinjoin_analysis"]["blocksci_heuristic_explanation"]
        self.assertEqual(explanation["heuristic"], "joinmarket_possible")
        self.assertIn("two_bucket_subset_after_fee", [rule["name"] for rule in explanation["rules"]])

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
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), "wasabi2")

        tx_record = report["transactions"]["txA"]["coinjoin_analysis"]
        self.assertEqual(tx_record["block_height"], 226)
        self.assertTrue(tx_record["block_height_inferred"])
        self.assertEqual(report["divergences"]["missed_by_blocksci"][0]["coinjoin_analysis"]["block_height"], 226)
        self.assertTrue(
            report["divergences"]["missed_by_blocksci"][0]["coinjoin_analysis"]["block_height_inferred"]
        )
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
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), "wasabi2")

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

    def test_exported_block_script_metadata_explains_taproot_witness_unknown(self):
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
                run_dir,
                coinjoin_analysis,
                blocksci_fixture("txB"),
                "wasabi2",
                min_input_count=1,
                test_values=True,
            )

        tx_record = report["transactions"]["txA"]["coinjoin_analysis"]
        self.assertEqual(tx_record["inputs"][0]["script_type"], "witness_v0_keyhash")
        self.assertEqual(tx_record["inputs"][0]["address_type"], "WITNESS_PUBKEYHASH")
        self.assertEqual(tx_record["outputs"][0]["address_type"], "WITNESS_PUBKEYHASH")
        self.assertEqual(tx_record["outputs"][1]["script_type"], "witness_v1_taproot")
        self.assertEqual(tx_record["outputs"][1]["address_type"], "WITNESS_UNKNOWN")

        explanation = tx_record["blocksci_heuristic_explanation"]
        output_type_rule = next(rule for rule in explanation["rules"] if rule["name"] == "output_address_types")
        self.assertTrue(output_type_rule["passed"])
        self.assertEqual(
            output_type_rule["observed"],
            "WITNESS_PUBKEYHASH, WITNESS_UNKNOWN (taproot/witness_v1_taproot)",
        )

        markdown = render_report(report)
        self.assertIn(
            "| output_address_types | yes | WITNESS_PUBKEYHASH, "
            "WITNESS_UNKNOWN (taproot/witness_v1_taproot) |",
            markdown,
        )
        self.assertIn(
            "BlockSci classifies taproot / witness v1 outputs as WITNESS_UNKNOWN in this build.",
            markdown,
        )
        self.assertIn("| 1 | 50,000 | output-a1 | witness_v1_taproot | WITNESS_UNKNOWN |", markdown)

    def test_build_report_field_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            blocksci_records = blocksci_fixture()
            blocksci_records["txA"]["outputs"][1]["value"] = 40000

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            report = build_report(run_dir, coinjoin_analysis, blocksci_records, "wasabi2")

        mismatches = report["transactions"]["txA"]["comparison"]["field_mismatches"]
        self.assertIn("outputs[1].value: coinjoin_analysis=50000, blocksci=40000", mismatches)
        self.assertEqual(report["summary"]["divergence_counts"]["shared_tx_mismatches"], 1)
        self.assertEqual(report["divergences"]["shared_tx_mismatches"][0]["txid"], "txA")
        self.assertEqual(report["divergences"]["shared_tx_mismatches"][0]["mismatch_count"], len(mismatches))

    def test_render_markdown_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_fixture())
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), "wasabi2")

        markdown = render_report(report)

        self.assertIn("# BlockSci vs Emulator CoinJoin Report", markdown)
        self.assertIn("| run time | - |", markdown)
        self.assertIn("| coinjoin-analysis coinjoins | 1 |", markdown)
        self.assertIn("## Divergence Overview", markdown)
        self.assertIn("| missed_by_blocksci | [txA](http://localhost:3002/tx/txA) |", markdown)
        self.assertIn("| blocksci_only | [txB](http://localhost:3002/tx/txB) |", markdown)
        self.assertIn("## Missed By BlockSci", markdown)
        self.assertIn("## Missed By BlockSci Details", markdown)
        self.assertIn("#### BlockSci Heuristic Explanation", markdown)
        self.assertIn("Python mirror result: fail", markdown)
        self.assertIn("unique_input_addresses", markdown)
        self.assertIn("#### Inputs", markdown)
        self.assertIn("#### Outputs", markdown)
        self.assertIn("wallet-000", markdown)

    def test_render_markdown_integration_ok_before_run(self):
        diagnostics = {
            "status": "ok",
            "problems": [],
            "images": {
                "blocksci": {"status": "ok"},
                "coinjoin_analysis": {"status": "ok"},
                "coinjoin_emulator": {"status": "ok"},
                "wrapper": {"status": "ok"},
            },
            "chain": {
                "status": "ok",
                "blocksci_chain_height": 1,
                "max_exported_block_height": 1,
            },
            "target_txids": {
                "status": "ok",
                "total": 2,
                "present": 2,
                "height_mismatches": 0,
            },
            "detector": {
                "status": "ok",
                "checked": 2,
                "disagreements": 0,
                "timeouts": 0,
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            report = build_report(
                run_dir,
                normalize_coinjoin_analysis(coinjoin_analysis_fixture()),
                blocksci_fixture(),
                "wasabi2",
                integration_diagnostics=diagnostics,
            )

        markdown = render_report(report)

        self.assertIn("## Inner Report Integration", markdown)
        self.assertIn("Inner report integration is OK.", markdown)
        self.assertLess(markdown.index("## Inner Report Integration"), markdown.index("## Run"))

    def test_render_markdown_integration_not_ok_lists_problems(self):
        diagnostics = {
            "status": "not_ok",
            "problems": ["BlockSci chain height 1 does not match max exported block height 2"],
            "images": {
                "blocksci": {"status": "ok"},
                "coinjoin_analysis": {"status": "ok"},
                "coinjoin_emulator": {"status": "ok"},
                "wrapper": {"status": "ok"},
            },
            "chain": {
                "status": "not_ok",
                "blocksci_chain_height": 1,
                "max_exported_block_height": 2,
            },
            "target_txids": {
                "status": "ok",
                "total": 0,
                "present": 0,
                "height_mismatches": 0,
            },
            "detector": {
                "status": "unavailable",
                "checked": 0,
                "disagreements": 0,
                "timeouts": 0,
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            report = build_report(
                run_dir,
                normalize_coinjoin_analysis(coinjoin_analysis_fixture()),
                blocksci_fixture(),
                "wasabi2",
                integration_diagnostics=diagnostics,
            )

        markdown = render_report(report)

        self.assertIn("Inner report integration is NOT OK.", markdown)
        self.assertIn("- BlockSci chain height 1 does not match max exported block height 2", markdown)

    def test_render_markdown_pass_but_missed_heuristic_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            coinjoin_analysis = normalize_coinjoin_analysis(wasabi2_passing_coinjoin_fixture())
            report = build_report(
                run_dir,
                coinjoin_analysis,
                blocksci_fixture("txB"),
                "wasabi2",
                min_input_count=1,
                test_values=True,
            )

        markdown = render_report(report)

        self.assertIn("Python mirror result: pass", markdown)
        self.assertIn("Failed rules: -", markdown)
        self.assertIn(
            "Python mirror passes; likely difference is unavailable address-type data, "
            "denomination set mismatch, or BlockSci configuration/threshold behavior.",
            markdown,
        )

    def test_render_markdown_joinmarket_heuristic_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            coinjoin_analysis = normalize_coinjoin_analysis(joinmarket_passing_coinjoin_fixture())
            report = build_report(run_dir, coinjoin_analysis, {}, "joinmarket")

        markdown = render_report(report)

        self.assertIn("#### BlockSci Heuristic Explanation", markdown)
        self.assertIn("joinmarket_definite", json.dumps(report))
        self.assertIn("subset_partition_after_fee", markdown)
        self.assertIn("Python mirror passes; likely difference is runtime BlockSci image/cache mismatch", markdown)

    def test_render_markdown_skipped_joinmarket_transactions_and_digests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            report = build_report(
                run_dir,
                normalize_coinjoin_analysis(joinmarket_passing_coinjoin_fixture()),
                {},
                "joinmarket",
                blocksci_skipped_txids=["skipped-tx"],
                blocksci_image_digest="sha256:blocksci",
            )

        markdown = render_report(report)

        self.assertIn("## BlockSci Skipped Transactions", markdown)
        self.assertIn("[skipped-tx](http://localhost:3002/tx/skipped-tx)", markdown)
        self.assertIn("| BlockSci image digest | sha256:blocksci |", markdown)

    def test_render_io_details_links_prev_txids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()

            fixture = coinjoin_analysis_fixture()
            fixture["coinjoins"]["txA"]["inputs"]["0"]["txid"] = (
                "4661b71b085395008966182375dd3b74d3b0c2f2617bfade403520da616bdb37"
            )
            coinjoin_analysis = normalize_coinjoin_analysis(fixture)
            report = build_report(run_dir, coinjoin_analysis, blocksci_fixture("txB"), "wasabi2")

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
                '{\n'
                '  "a": {\n'
                '    "c": 3,\n'
                '    "d": 4\n'
                '  },\n'
                '  "b": 1\n'
                '}\n',
            )

    def test_normalize_scenario(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scenario_path = Path(tmpdir) / "defaultCoinJoin.json"
            scenario = normalize_scenario(scenario_fixture(), scenario_path)

        self.assertEqual(scenario["source"], str(scenario_path))
        self.assertEqual(scenario["sha256"], sha256_json(scenario_fixture()))
        self.assertEqual(scenario["name"], "default")
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

    def test_load_scenario_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            run_dir = base / "run-without-scenario"
            run_dir.mkdir()
            fallback_path = base / "defaultCoinJoin.json"
            fallback_path.write_text(json.dumps(scenario_fixture()), encoding="utf-8")

            scenario = load_scenario(run_dir, fallback_path)

        assert scenario is not None
        self.assertEqual(scenario["source"], str(fallback_path))
        self.assertEqual(scenario["sha256"], sha256_json(scenario_fixture()))

    def test_false_positive_sidecars_are_merged_and_filter_only_baseline_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            analysis_dir = Path(tmpdir)
            (analysis_dir / "false_cjtxs.json").write_text(
                json.dumps({"manual": ["txA", "not-in-baseline"]}), encoding="utf-8"
            )
            (analysis_dir / "false_cjtxs.json.reviewed").write_text(
                json.dumps({"reuse": ["txA"]}), encoding="utf-8"
            )

            txids, sources = load_false_positive_txids(analysis_dir)
            filtered, removed = filter_coinjoin_analysis_false_positives(
                coinjoin_analysis_fixture(), txids
            )

        self.assertEqual(txids, {"txA", "not-in-baseline"})
        self.assertEqual([source["file"] for source in sources], [
            "false_cjtxs.json", "false_cjtxs.json.reviewed"
        ])
        self.assertEqual(removed, ["txA"])
        self.assertNotIn("txA", filtered["coinjoins"])

    def test_mapping_results_are_summarized_and_timeout_marks_partial(self):
        mappings = {
            "status": "complete",
            "provenance": {
                "enumerator_image": "docker://enumerator",
                "sake_image": "docker://sake",
                "enumerator_image_digest": "sha256:enum",
                "sake_image_digest": "sha256:sake",
            },
            "enumerator": {
                "parameters": {"mode": "numeric"},
                "summary": {"transactions": 1, "completed": 0, "timed_out": 1, "errors": 0},
                "transactions": {"txA": {"status": "timeout", "mapping_count": None, "retried": True}},
            },
            "sake": {
                "seed": 42,
                "summary": {"output_match_rate": 0.5, "wallet_match_rate": 0.25,
                            "length_match_rate": 0.75, "full_coinjoin_match_rate": 0.0},
                "transactions": {},
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            report = build_report(Path(tmpdir) / "run", {}, {}, "wasabi2", coinjoin_mappings=mappings)

        self.assertEqual(report["coinjoin_mappings"]["status"], "partial")
        self.assertEqual(report["summary"]["mapping_timed_out"], 1)
        self.assertEqual(report["run_manifest"]["image_digests"]["sake"], "sha256:sake")
        self.assertIn("Sake length match rate", render_report(report))

    def test_report_without_mapping_artifact_keeps_mapping_section_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = build_report(Path(tmpdir) / "run", {}, {}, "wasabi2")
        self.assertIsNone(report["coinjoin_mappings"])
        self.assertNotIn("mapping_transactions", report["summary"])


if __name__ == "__main__":
    unittest.main()
