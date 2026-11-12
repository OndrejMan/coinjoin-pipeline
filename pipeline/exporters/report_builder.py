"""Assemble the unified report document."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from exporters.common import (
    SCHEMA_VERSION,
    JsonObject,
    compute_rate,
    parse_run_started_at,
)
from exporters.comparison import (
    build_divergences,
    compare_records,
)
from exporters.parameters import Detector
from exporters.report_types import TransactionRecord

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


def build_summary(
    transactions: JsonObject,
    coinjoin_analysis: dict[str, TransactionRecord],
    blocksci_records: dict[str, TransactionRecord],
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
    }
    return summary


def build_report(
    run_dir: Path,
    coinjoin_analysis: dict[str, TransactionRecord],
    blocksci_records: dict[str, TransactionRecord],
    detector: Detector,
    scenario: JsonObject | None = None,
) -> JsonObject:
    """The unified report. Every top-level key is always present, null when it does not apply."""
    coinjoin_type, min_input_count = detector.coinjoin_type, detector.min_input_count
    transactions = compare_transactions(coinjoin_analysis, blocksci_records)
    summary = build_summary(transactions, coinjoin_analysis, blocksci_records)
    divergences = build_divergences(transactions)
    summary["divergence_counts"] = {name: len(items) for name, items in divergences.items()}
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
        "scenario": scenario,
        "summary": summary,
        "divergences": divergences,
        "transactions": transactions,
    }
