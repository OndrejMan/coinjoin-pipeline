"""Assemble the unified report document."""

from __future__ import annotations

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
    parse_run_started_at,
)
from exporters.comparison import (
    build_detection_confusion_matrix,
    build_divergences,
    compare_records,
    compute_rate,
    evaluate_cluster_assignments,
)
from exporters.heuristics import add_blocksci_heuristic_explanations
from exporters.manifest import build_run_manifest, compare_run_manifests
from exporters.scenario import build_scenario_checks
from exporters.script_metadata import enrich_records_with_script_metadata


def build_report(
    run_dir: Path,
    coinjoin_analysis: dict[str, JsonObject],
    blocksci_records: dict[str, JsonObject],
    coinjoin_type: str,
    scenario: JsonObject | None = None,
    min_input_count: int | None = None,
    test_values: bool = False,
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
    wrapper_image: str | None = None,
    blocksci_image_digest: str | None = None,
    coinjoin_analysis_image_digest: str | None = None,
    coinjoin_emulator_image_digest: str | None = None,
    wrapper_image_digest: str | None = None,
    emulator_git_commit: str | None = None,
    previous_run_manifest: JsonObject | None = None,
    integration_diagnostics: JsonObject | None = None,
    mode: str = "emulator",
    network: str | None = None,
    coinjoin_mappings: JsonObject | None = None,
) -> JsonObject:
    if coinjoin_mappings:
        enumerator_summary = (coinjoin_mappings.get("enumerator") or {}).get("summary") or {}
        sake_summary = (coinjoin_mappings.get("sake") or {}).get("summary") or {}
        if (
            any((
                enumerator_summary.get("timed_out"),
                enumerator_summary.get("errors"),
                sake_summary.get("errors"),
            ))
            and coinjoin_mappings.get("status") == "complete"
        ):
            coinjoin_mappings = {**coinjoin_mappings, "status": "partial"}
    enrich_records_with_script_metadata(coinjoin_analysis, run_dir)
    enrich_records_with_script_metadata(blocksci_records, run_dir)
    add_blocksci_heuristic_explanations(
        coinjoin_analysis,
        coinjoin_type,
        min_input_count=min_input_count,
        test_values=test_values,
        first_wasabi2_block=first_wasabi2_block,
        joinmarket_detector=joinmarket_detector,
        joinmarket_min_base_fee=joinmarket_min_base_fee,
        joinmarket_percentage_fee=joinmarket_percentage_fee,
        joinmarket_max_depth=joinmarket_max_depth,
    )
    all_txids = sorted(set(coinjoin_analysis) | set(blocksci_records))
    transactions: JsonObject = {}

    matched_by_both = 0
    blocksci_only = 0
    missed_by_blocksci = 0

    for txid in all_txids:
        coinjoin_analysis_record = coinjoin_analysis.get(txid)
        blocksci_record = blocksci_records.get(txid)
        if coinjoin_analysis_record is not None and blocksci_record is not None:
            status = "matched_by_both"
            matched_by_both += 1
            field_mismatches = compare_records(coinjoin_analysis_record, blocksci_record)
        elif coinjoin_analysis_record is not None:
            status = "missed_by_blocksci"
            missed_by_blocksci += 1
            field_mismatches = []
        else:
            status = "blocksci_only"
            blocksci_only += 1
            field_mismatches = []

        transactions[txid] = {
            "comparison": {
                "status": status,
                "field_mismatches": field_mismatches,
            },
            "coinjoin_analysis": coinjoin_analysis_record,
            "blocksci": blocksci_record,
        }

    detected_count = len(blocksci_records)
    coinjoin_analysis_count = len(coinjoin_analysis)
    detection_confusion_matrix = build_detection_confusion_matrix(emulator_data, blocksci_records)
    independent_emulator_labels = bool(
        emulator_data and (emulator_data.get("label_provenance") or {}).get("independent")
    )
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
        and not test_values
        and not blocksci_records
        and emulator_block_heights
        and max(emulator_block_heights) < WASABI2_THRESHOLD_CHANGE_BLOCK
    ):
        warnings.append(
            {
                "code": "wasabi_production_threshold_zero_detections",
                "message": (
                    "BlockSci detected no Wasabi2 CoinJoins at regtest-height blocks while using "
                    "the production minimum-input threshold. Use --test-values explicitly for "
                    "small emulated rounds, or keep this run as a production-threshold comparison."
                ),
            }
        )
    clustering_evaluation = evaluate_cluster_assignments(
        emulator_data,
        predicted_address_clusters,
        unavailable_reason=cluster_export_error,
    )
    summary = {
        "coinjoin_analysis_coinjoins": coinjoin_analysis_count,
        "blocksci_detected_coinjoins": detected_count,
        "matched_by_both": matched_by_both,
        "blocksci_only": blocksci_only,
        "missed_by_blocksci": missed_by_blocksci,
        "blocksci_agreement_rate": compute_rate(
            matched_by_both,
            detected_count,
            1.0 if coinjoin_analysis_count == 0 else 0.0,
        ),
        "coinjoin_analysis_coverage_by_blocksci": compute_rate(matched_by_both, coinjoin_analysis_count, 1.0),
        "scenario_checks": build_scenario_checks(scenario, coinjoin_analysis),
    }
    if emulator_data:
        emulator_summary = emulator_data.get("summary") or {}
        summary["emulator_data_transactions"] = emulator_summary.get("transactions")
        summary["emulator_data_coinjoins"] = emulator_summary.get("coinjoin_transactions")
        summary["emulator_data_unknown_transactions"] = emulator_summary.get("unknown_transactions")
    if coinjoin_type == "joinmarket":
        summary["blocksci_joinmarket_skipped"] = len(blocksci_skipped_txids or [])
    if coinjoin_mappings:
        enumerator_summary = (coinjoin_mappings.get("enumerator") or {}).get("summary") or {}
        sake_summary = (coinjoin_mappings.get("sake") or {}).get("summary") or {}
        summary.update({
            "mapping_transactions": enumerator_summary.get("transactions"),
            "mapping_completed": enumerator_summary.get("completed"),
            "mapping_timed_out": enumerator_summary.get("timed_out"),
            "mapping_errors": enumerator_summary.get("errors"),
            "sake_output_match_rate": sake_summary.get("output_match_rate"),
            "sake_wallet_match_rate": sake_summary.get("wallet_match_rate"),
            "sake_full_coinjoin_match_rate": sake_summary.get("full_coinjoin_match_rate"),
        })
    divergences = build_divergences(transactions)
    summary["divergence_counts"] = {name: len(items) for name, items in divergences.items()}
    run_manifest = build_run_manifest(
        run_dir,
        scenario,
        coinjoin_type,
        engine,
        min_input_count,
        test_values,
        first_wasabi2_block,
        joinmarket_detector,
        joinmarket_min_base_fee,
        joinmarket_percentage_fee,
        joinmarket_max_depth,
        blocksci_image=blocksci_image,
        coinjoin_analysis_image=coinjoin_analysis_image,
        coinjoin_emulator_image=coinjoin_emulator_image,
        wrapper_image=wrapper_image,
        blocksci_image_digest=blocksci_image_digest,
        coinjoin_analysis_image_digest=coinjoin_analysis_image_digest,
        coinjoin_emulator_image_digest=coinjoin_emulator_image_digest,
        wrapper_image_digest=wrapper_image_digest,
        emulator_git_commit=emulator_git_commit,
    )
    run_manifest["mode"] = mode
    run_manifest["network"] = network
    if coinjoin_mappings:
        provenance = coinjoin_mappings.get("provenance") or {}
        run_manifest["images"].update({
            "mappings_enumerator": provenance.get("enumerator_image"),
            "sake": provenance.get("sake_image"),
        })
        run_manifest["image_digests"].update({
            "mappings_enumerator": provenance.get("enumerator_image_digest"),
            "sake": provenance.get("sake_image_digest"),
        })
        run_manifest["mapping_parameters"] = (coinjoin_mappings.get("enumerator") or {}).get("parameters")
        run_manifest["sake_seed"] = (coinjoin_mappings.get("sake") or {}).get("seed")

    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "id": run_dir.name,
            "started_at": parse_run_started_at(run_dir.name),
            "scenario_name": scenario.get("name") if scenario else None,
            "coinjoin_type": coinjoin_type,
            "blocksci_min_input_count": min_input_count,
            "blocksci_test_values": test_values,
            "first_wasabi2_block": first_wasabi2_block if coinjoin_type == "wasabi2" else None,
            "joinmarket_detector": joinmarket_detector if coinjoin_type == "joinmarket" else None,
            "joinmarket_min_base_fee": joinmarket_min_base_fee if coinjoin_type == "joinmarket" else None,
            "joinmarket_percentage_fee": joinmarket_percentage_fee if coinjoin_type == "joinmarket" else None,
            "joinmarket_max_depth": joinmarket_max_depth if coinjoin_type == "joinmarket" else None,
            "scenario_sha256": scenario.get("sha256") if scenario else None,
            "mode": mode,
            "network": network,
        },
        "run_manifest": run_manifest,
        "run_manifest_comparison": compare_run_manifests(previous_run_manifest, run_manifest),
        "warnings": warnings,
        "integration_diagnostics": integration_diagnostics,
        "scenario": scenario,
        "emulator_data": {
            "schema_version": emulator_data.get("schema_version"),
            "summary": emulator_data.get("summary"),
            "label_provenance": emulator_data.get("label_provenance"),
        } if emulator_data else None,
        "detection_confusion_matrix": detection_confusion_matrix,
        "evaluation_scope": (
            "baseline_agreement_only"
            if mode == "external"
            else "emulator_ground_truth"
            if independent_emulator_labels
            else "emulator_labels_unavailable"
        ),
        "clustering_evaluation": clustering_evaluation,
        "blocksci_skipped_txids": blocksci_skipped_txids or [],
        "coinjoin_mappings": coinjoin_mappings,
        "summary": summary,
        "divergences": divergences,
        "transactions": transactions,
    }
