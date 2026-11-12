"""Assemble the unified report document."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from exporters.common import (
    SCHEMA_VERSION,
    WASABI2_THRESHOLD_CHANGE_BLOCK,
    JsonObject,
    compute_rate,
    parse_run_started_at,
)
from exporters.comparison import (
    build_detection_confusion_matrix,
    build_divergences,
    compare_records,
    evaluate_cluster_assignments,
)
from exporters.heuristics import add_blocksci_heuristic_explanations
from exporters.manifest import build_run_manifest, compare_run_manifests
from exporters.parameters import Detector, ReportImages
from exporters.report_types import SubsetMatchingAnalysis, TransactionRecord
from exporters.scenario import build_scenario_checks
from exporters.script_metadata import enrich_records_with_script_metadata, load_exported_block_script_metadata

MATCHED = "matched_by_both"
MISSED = "missed_by_blocksci"
BLOCKSCI_ONLY = "blocksci_only"


def compare_transactions(
    coinjoin_analysis: dict[str, TransactionRecord],
    blocksci_records: dict[str, TransactionRecord],
) -> JsonObject:
    """Both analyzers' record of every txid either reported, with the comparison status."""
    transactions: JsonObject = {}
    for txid in sorted(set(coinjoin_analysis) | set(blocksci_records)):
        coinjoin_analysis_record = coinjoin_analysis.get(txid)
        blocksci_record = blocksci_records.get(txid)
        field_mismatches: list[str] = []
        if coinjoin_analysis_record is not None and blocksci_record is not None:
            status = MATCHED
            field_mismatches = compare_records(coinjoin_analysis_record, blocksci_record)
        elif coinjoin_analysis_record is not None:
            status = MISSED
        else:
            status = BLOCKSCI_ONLY
        transactions[txid] = {
            "comparison": {"status": status, "field_mismatches": field_mismatches},
            "coinjoin_analysis": coinjoin_analysis_record,
            "blocksci": blocksci_record,
        }
    return transactions


def build_warnings(
    coinjoin_type: str,
    min_input_count: int | None,
    blocksci_records: dict[str, TransactionRecord],
    emulator_data: JsonObject | None,
) -> list[JsonObject]:
    warnings: list[JsonObject] = []
    emulator_block_heights = [
        int(record["block_height"])
        for record in ((emulator_data or {}).get("transactions") or {}).values()
        if record.get("block_height") is not None
    ]
    if (
        coinjoin_type == "wasabi2"
        and min_input_count is None
        and not blocksci_records
        and emulator_block_heights
        and max(emulator_block_heights) < WASABI2_THRESHOLD_CHANGE_BLOCK
    ):
        warnings.append(
            {
                "code": "wasabi_production_threshold_zero_detections",
                "message": (
                    "BlockSci detected no Wasabi2 CoinJoins at regtest-height blocks while using "
                    "the production minimum-input threshold. Use an explicit --min-input-count "
                    "for small emulated rounds, or keep this run as a production-threshold comparison."
                ),
            }
        )
    return warnings


def evaluation_scope(independent_emulator_labels: bool) -> str:
    return "emulator_ground_truth" if independent_emulator_labels else "emulator_labels_unavailable"


def build_summary(
    transactions: JsonObject,
    coinjoin_analysis: dict[str, TransactionRecord],
    blocksci_records: dict[str, TransactionRecord],
    coinjoin_type: str,
    scenario: JsonObject | None,
    emulator_data: JsonObject | None,
) -> JsonObject:
    statuses = Counter(transaction["comparison"]["status"] for transaction in transactions.values())
    detected_count = len(blocksci_records)
    coinjoin_analysis_count = len(coinjoin_analysis)
    summary: JsonObject = {
        "coinjoin_analysis_coinjoins": coinjoin_analysis_count,
        "blocksci_detected_coinjoins": detected_count,
        MATCHED: statuses[MATCHED],
        BLOCKSCI_ONLY: statuses[BLOCKSCI_ONLY],
        MISSED: statuses[MISSED],
        "blocksci_agreement_rate": compute_rate(
            statuses[MATCHED],
            detected_count,
            1.0 if coinjoin_analysis_count == 0 else 0.0,
        ),
        "coinjoin_analysis_coverage_by_blocksci": compute_rate(statuses[MATCHED], coinjoin_analysis_count, 1.0),
        "scenario_checks": build_scenario_checks(scenario, coinjoin_analysis, coinjoin_type),
    }
    if emulator_data:
        emulator_summary = emulator_data.get("summary") or {}
        summary["emulator_data_transactions"] = emulator_summary.get("transactions")
        summary["emulator_data_coinjoins"] = emulator_summary.get("coinjoin_transactions")
        summary["emulator_data_unknown_transactions"] = emulator_summary.get("unknown_transactions")
    return summary


def build_subset_matching_comparison(
    subset_matching: SubsetMatchingAnalysis | None,
    coinjoin_analysis: dict[str, TransactionRecord],
    blocksci_records: dict[str, TransactionRecord],
    detector: Detector,
) -> JsonObject | None:
    """The deprecated subset-matching JoinMarket scan against coinjoin-analysis and the protocol heuristic."""
    if subset_matching is None:
        return None
    detected = set(subset_matching["records"])
    baseline = set(coinjoin_analysis)
    heuristic = set(blocksci_records)
    matched = detected & baseline
    return {
        "deprecated": True,
        "reason": (
            "BlockSci deprecates the input-subset-matching detectors: their backtracking is exponential in the "
            "number of inputs and impractically slow on mainnet. The report's BlockSci detection uses the "
            "JoinMarket protocol heuristic instead."
        ),
        "parameters": {
            "detector": detector.joinmarket_detector,
            "min_base_fee": detector.joinmarket_min_base_fee,
            "percentage_fee": detector.joinmarket_percentage_fee,
            "max_depth": detector.joinmarket_max_depth,
        },
        "summary": {
            "detected_coinjoins": len(detected),
            MATCHED: len(matched),
            BLOCKSCI_ONLY: len(detected - baseline),
            MISSED: len(baseline - detected),
            "agreement_rate": compute_rate(len(matched), len(detected), 1.0 if not baseline else 0.0),
            "coinjoin_analysis_coverage": compute_rate(len(matched), len(baseline), 1.0),
            "skipped": len(subset_matching["skipped_txids"]),
            "also_detected_by_blocksci_heuristic": len(detected & heuristic),
            "subset_matching_only": len(detected - heuristic),
            "blocksci_heuristic_only": len(heuristic - detected),
        },
        "blocksci_only_txids": sorted(detected - baseline),
        "missed_by_blocksci_txids": sorted(baseline - detected),
        "skipped_txids": subset_matching["skipped_txids"],
    }


def build_report(
    run_dir: Path,
    coinjoin_analysis: dict[str, TransactionRecord],
    blocksci_records: dict[str, TransactionRecord],
    detector: Detector,
    scenario: JsonObject | None = None,
    *,
    emulator_data: JsonObject | None = None,
    predicted_address_clusters: dict[str, str] | None = None,
    cluster_export_error: str | None = None,
    subset_matching: SubsetMatchingAnalysis | None = None,
    engine: str | None = None,
    images: ReportImages | None = None,
    emulator_git_commit: str | None = None,
    previous_run_manifest: JsonObject | None = None,
    integration_diagnostics: JsonObject | None = None,
    image_provenance: JsonObject | None = None,
) -> JsonObject:
    """The unified report. Every top-level key is always present, null when it does not apply."""
    coinjoin_type, min_input_count = detector.coinjoin_type, detector.min_input_count
    # One read of the exported blocks serves both analyzers' records.
    script_index = load_exported_block_script_metadata(run_dir)
    enrich_records_with_script_metadata(coinjoin_analysis, run_dir, script_index)
    enrich_records_with_script_metadata(blocksci_records, run_dir, script_index)
    add_blocksci_heuristic_explanations(coinjoin_analysis, detector)
    transactions = compare_transactions(coinjoin_analysis, blocksci_records)

    independent_emulator_labels = bool(
        emulator_data and (emulator_data.get("label_provenance") or {}).get("independent")
    )
    detector_evaluations = (
        {
            "blocksci": build_detection_confusion_matrix(emulator_data, blocksci_records),
            "coinjoin_analysis": build_detection_confusion_matrix(emulator_data, coinjoin_analysis),
        }
        if independent_emulator_labels
        else None
    )
    if detector_evaluations is not None and subset_matching is not None:
        detector_evaluations["blocksci_subset_matching"] = build_detection_confusion_matrix(
            emulator_data, subset_matching["records"]
        )
    summary = build_summary(
        transactions,
        coinjoin_analysis,
        blocksci_records,
        coinjoin_type,
        scenario,
        emulator_data,
    )
    divergences = build_divergences(transactions)
    summary["divergence_counts"] = {name: len(items) for name, items in divergences.items()}
    run_manifest = build_run_manifest(
        run_dir,
        scenario,
        detector,
        engine,
        images,
        emulator_git_commit=emulator_git_commit,
        image_provenance=image_provenance,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "id": run_dir.name,
            "started_at": parse_run_started_at(run_dir.name),
            "scenario_name": scenario.get("name") if scenario else None,
            "coinjoin_type": coinjoin_type,
            "blocksci_min_input_count": min_input_count,
            "scenario_sha256": scenario.get("sha256") if scenario else None,
        },
        "run_manifest": run_manifest,
        "run_manifest_comparison": compare_run_manifests(previous_run_manifest, run_manifest),
        "warnings": build_warnings(coinjoin_type, min_input_count, blocksci_records, emulator_data),
        "integration_diagnostics": integration_diagnostics,
        "scenario": scenario,
        "emulator_data": {
            "schema_version": emulator_data.get("schema_version"),
            "summary": emulator_data.get("summary"),
            "label_provenance": emulator_data.get("label_provenance"),
        }
        if emulator_data
        else None,
        "detector_evaluations": detector_evaluations,
        "evaluation_scope": evaluation_scope(independent_emulator_labels),
        "clustering_evaluation": evaluate_cluster_assignments(
            emulator_data,
            predicted_address_clusters,
            unavailable_reason=cluster_export_error,
        ),
        "blocksci_subset_matching": build_subset_matching_comparison(
            subset_matching, coinjoin_analysis, blocksci_records, detector
        ),
        "summary": summary,
        "divergences": divergences,
        "transactions": transactions,
    }
