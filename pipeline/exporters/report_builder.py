"""Assemble the unified report document."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from exporters.common import (
    DEFAULT_FIRST_WASABI2_BLOCK,
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
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
from exporters.manifest import apply_mapping_provenance, build_run_manifest, compare_run_manifests
from exporters.normalization import baseline_filter_summary
from exporters.report_types import TransactionRecord
from exporters.scenario import build_scenario_checks
from exporters.script_metadata import enrich_records_with_script_metadata, load_exported_block_script_metadata

MATCHED = "matched_by_both"
MISSED = "missed_by_blocksci"
BLOCKSCI_ONLY = "blocksci_only"


def mapping_summaries(coinjoin_mappings: JsonObject | None) -> tuple[JsonObject, JsonObject]:
    """The enumerator and Sake summaries of the mapping stage, empty when absent."""
    mappings = coinjoin_mappings or {}
    return (
        (mappings.get("enumerator") or {}).get("summary") or {},
        (mappings.get("sake") or {}).get("summary") or {},
    )


def with_effective_mapping_status(coinjoin_mappings: JsonObject | None) -> JsonObject | None:
    """A "complete" mapping stage whose transactions timed out or failed is only partial."""
    if not coinjoin_mappings or coinjoin_mappings.get("status") != "complete":
        return coinjoin_mappings
    enumerator_summary, sake_summary = mapping_summaries(coinjoin_mappings)
    if any((enumerator_summary.get("timed_out"), enumerator_summary.get("errors"), sake_summary.get("errors"))):
        return {**coinjoin_mappings, "status": "partial"}
    return coinjoin_mappings


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
    mode: str,
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
        mode == "emulator"
        and coinjoin_type == "wasabi2"
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


def evaluation_scope(mode: str, independent_emulator_labels: bool) -> str:
    if mode == "external":
        return "baseline_agreement_only"
    return "emulator_ground_truth" if independent_emulator_labels else "emulator_labels_unavailable"


def build_summary(
    transactions: JsonObject,
    coinjoin_analysis: dict[str, TransactionRecord],
    blocksci_records: dict[str, TransactionRecord],
    coinjoin_type: str,
    scenario: JsonObject | None,
    emulator_data: JsonObject | None,
    blocksci_skipped_txids: list[str],
    coinjoin_mappings: JsonObject | None,
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
    if coinjoin_type == "joinmarket":
        summary["blocksci_joinmarket_skipped"] = len(blocksci_skipped_txids)
    if coinjoin_mappings:
        enumerator_summary, sake_summary = mapping_summaries(coinjoin_mappings)
        summary.update(
            {
                "mapping_transactions": enumerator_summary.get("transactions"),
                "mapping_completed": enumerator_summary.get("completed"),
                "mapping_timed_out": enumerator_summary.get("timed_out"),
                "mapping_errors": enumerator_summary.get("errors"),
                "sake_output_match_rate": sake_summary.get("output_match_rate"),
                "sake_wallet_match_rate": sake_summary.get("wallet_match_rate"),
                "sake_full_coinjoin_match_rate": sake_summary.get("full_coinjoin_match_rate"),
            }
        )
    return summary


def build_report(
    run_dir: Path,
    coinjoin_analysis: dict[str, TransactionRecord],
    blocksci_records: dict[str, TransactionRecord],
    coinjoin_type: str,
    scenario: JsonObject | None = None,
    min_input_count: int | None = None,
    first_wasabi2_block: int = DEFAULT_FIRST_WASABI2_BLOCK,
    emulator_data: JsonObject | None = None,
    predicted_address_clusters: dict[str, str] | None = None,
    cluster_export_error: str | None = None,
    blocksci_skipped_txids: list[str] | None = None,
    joinmarket_detector: str = DEFAULT_JOINMARKET_DETECTOR,
    joinmarket_min_base_fee: int = DEFAULT_JOINMARKET_MIN_BASE_FEE,
    joinmarket_percentage_fee: float = DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    joinmarket_max_depth: int = DEFAULT_JOINMARKET_MAX_DEPTH,
    engine: str | None = None,
    blocksci_image: str | None = None,
    coinjoin_analysis_image: str | None = None,
    coinjoin_emulator_image: str | None = None,
    uploader_image: str | None = None,
    unified_report_image: str | None = None,
    blocksci_image_digest: str | None = None,
    coinjoin_analysis_image_digest: str | None = None,
    coinjoin_emulator_image_digest: str | None = None,
    uploader_image_digest: str | None = None,
    unified_report_image_digest: str | None = None,
    emulator_git_commit: str | None = None,
    previous_run_manifest: JsonObject | None = None,
    integration_diagnostics: JsonObject | None = None,
    mode: str = "emulator",
    network: str | None = None,
    coinjoin_mappings: JsonObject | None = None,
    image_provenance: JsonObject | None = None,
    coverage: JsonObject | None = None,
    baseline_filter: JsonObject | None = None,
    liquidity_evaluation: JsonObject | None = None,
) -> JsonObject:
    """The unified report. Every top-level key is always present, null when it does not apply."""
    coinjoin_mappings = with_effective_mapping_status(coinjoin_mappings)
    blocksci_skipped_txids = blocksci_skipped_txids or []
    # One read of the exported blocks serves both analyzers' records.
    script_index = load_exported_block_script_metadata(run_dir)
    enrich_records_with_script_metadata(coinjoin_analysis, run_dir, script_index)
    enrich_records_with_script_metadata(blocksci_records, run_dir, script_index)
    add_blocksci_heuristic_explanations(
        coinjoin_analysis,
        coinjoin_type,
        min_input_count=min_input_count,
        first_wasabi2_block=first_wasabi2_block,
        joinmarket_detector=joinmarket_detector,
        joinmarket_min_base_fee=joinmarket_min_base_fee,
        joinmarket_percentage_fee=joinmarket_percentage_fee,
        joinmarket_max_depth=joinmarket_max_depth,
    )
    transactions = compare_transactions(coinjoin_analysis, blocksci_records)

    blocksci_detection_confusion_matrix = build_detection_confusion_matrix(emulator_data, blocksci_records)
    independent_emulator_labels = bool(
        emulator_data and (emulator_data.get("label_provenance") or {}).get("independent")
    )
    detector_evaluations = (
        {
            "blocksci": blocksci_detection_confusion_matrix,
            "coinjoin_analysis": build_detection_confusion_matrix(emulator_data, coinjoin_analysis),
        }
        if independent_emulator_labels
        else None
    )
    summary = build_summary(
        transactions,
        coinjoin_analysis,
        blocksci_records,
        coinjoin_type,
        scenario,
        emulator_data,
        blocksci_skipped_txids,
        coinjoin_mappings,
    )
    divergences = build_divergences(transactions)
    summary["divergence_counts"] = {name: len(items) for name, items in divergences.items()}
    run_manifest = build_run_manifest(
        run_dir,
        scenario,
        coinjoin_type,
        engine,
        min_input_count,
        first_wasabi2_block,
        joinmarket_detector,
        joinmarket_min_base_fee,
        joinmarket_percentage_fee,
        joinmarket_max_depth,
        blocksci_image=blocksci_image,
        coinjoin_analysis_image=coinjoin_analysis_image,
        coinjoin_emulator_image=coinjoin_emulator_image,
        uploader_image=uploader_image,
        unified_report_image=unified_report_image,
        blocksci_image_digest=blocksci_image_digest,
        coinjoin_analysis_image_digest=coinjoin_analysis_image_digest,
        coinjoin_emulator_image_digest=coinjoin_emulator_image_digest,
        uploader_image_digest=uploader_image_digest,
        unified_report_image_digest=unified_report_image_digest,
        emulator_git_commit=emulator_git_commit,
        image_provenance=image_provenance,
    )
    run_manifest["mode"] = mode
    run_manifest["network"] = network
    apply_mapping_provenance(run_manifest, coinjoin_mappings)
    # The detector manifest holds only the selected type's parameters; the run
    # block spells every parameter out and leaves the other type's as null.
    detector = run_manifest["detector"]

    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "id": run_dir.name,
            "started_at": parse_run_started_at(run_dir.name),
            "scenario_name": scenario.get("name") if scenario else None,
            "coinjoin_type": coinjoin_type,
            "blocksci_min_input_count": min_input_count,
            "first_wasabi2_block": detector.get("first_wasabi2_block"),
            "joinmarket_detector": detector.get("joinmarket_detector"),
            "joinmarket_min_base_fee": detector.get("joinmarket_min_base_fee"),
            "joinmarket_percentage_fee": detector.get("joinmarket_percentage_fee"),
            "joinmarket_max_depth": detector.get("joinmarket_max_depth"),
            "scenario_sha256": scenario.get("sha256") if scenario else None,
            "mode": mode,
            "network": network,
        },
        "run_manifest": run_manifest,
        "run_manifest_comparison": compare_run_manifests(previous_run_manifest, run_manifest),
        "warnings": build_warnings(mode, coinjoin_type, min_input_count, blocksci_records, emulator_data),
        "integration_diagnostics": integration_diagnostics,
        "scenario": scenario,
        "emulator_data": {
            "schema_version": emulator_data.get("schema_version"),
            "summary": emulator_data.get("summary"),
            "label_provenance": emulator_data.get("label_provenance"),
        }
        if emulator_data
        else None,
        # Compatibility alias retained for schema 1.x consumers. New consumers
        # should read both analyzers from detector_evaluations.
        "detection_confusion_matrix": blocksci_detection_confusion_matrix,
        "detector_evaluations": detector_evaluations,
        "evaluation_scope": evaluation_scope(mode, independent_emulator_labels),
        "clustering_evaluation": evaluate_cluster_assignments(
            emulator_data,
            predicted_address_clusters,
            unavailable_reason=cluster_export_error,
        ),
        "blocksci_skipped_txids": blocksci_skipped_txids,
        "coinjoin_mappings": coinjoin_mappings,
        # External mode only: the BlockSci scan window and where the baseline sits in it.
        "coverage": coverage,
        "baseline_filter": baseline_filter if baseline_filter is not None else baseline_filter_summary([], set(), []),
        "liquidity_evaluation": liquidity_evaluation,
        "summary": summary,
        "divergences": divergences,
        "transactions": transactions,
    }
