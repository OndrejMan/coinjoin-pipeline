"""Comparison, divergence and detection metrics."""

from __future__ import annotations

from collections.abc import Mapping

from exporters.common import JsonObject, compute_optional_rate, f1_score, wallet_names
from exporters.report_types import DetectionConfusionMatrix, IORecord, RecordSummary, TransactionRecord


def compare_io(
    label: str,
    coinjoin_analysis_values: list[IORecord],
    blocksci_values: list[IORecord],
) -> list[str]:
    mismatches = []
    coinjoin_analysis_by_index = {item["index"]: item for item in coinjoin_analysis_values}
    blocksci_by_index = {item["index"]: item for item in blocksci_values}
    for index in sorted(set(coinjoin_analysis_by_index) | set(blocksci_by_index), key=int):
        coinjoin_analysis_item = coinjoin_analysis_by_index.get(index)
        blocksci_item = blocksci_by_index.get(index)
        if coinjoin_analysis_item is None:
            mismatches.append(f"{label}[{index}] only in blocksci")
            continue
        if blocksci_item is None:
            mismatches.append(f"{label}[{index}] only in coinjoin_analysis")
            continue
        for field in ("value", "address"):
            if coinjoin_analysis_item.get(field) != blocksci_item.get(field):
                mismatches.append(
                    f"{label}[{index}].{field}: "
                    f"coinjoin_analysis={coinjoin_analysis_item.get(field)!r}, "
                    f"blocksci={blocksci_item.get(field)!r}"
                )
    return mismatches


def compare_records(coinjoin_analysis_record: TransactionRecord, blocksci_record: TransactionRecord) -> list[str]:
    mismatches = []
    for field in (
        "input_count",
        "output_count",
        "total_input_sats",
        "total_output_sats",
        "repeated_output_denominations",
    ):
        if coinjoin_analysis_record.get(field) != blocksci_record.get(field):
            mismatches.append(
                f"{field}: coinjoin_analysis={coinjoin_analysis_record.get(field)!r}, "
                f"blocksci={blocksci_record.get(field)!r}"
            )
    mismatches.extend(
        compare_io("inputs", coinjoin_analysis_record.get("inputs", []), blocksci_record.get("inputs", []))
    )
    mismatches.extend(
        compare_io("outputs", coinjoin_analysis_record.get("outputs", []), blocksci_record.get("outputs", []))
    )
    return mismatches


def record_wallets(record: TransactionRecord | None) -> list[str]:
    return wallet_names(record)


def record_summary(record: TransactionRecord | None) -> RecordSummary | None:
    if record is None:
        return None

    return {
        "block_height": record.get("block_height"),
        "block_height_inferred": record.get("block_height_inferred"),
        "input_count": record.get("input_count"),
        "output_count": record.get("output_count"),
        "total_input_sats": record.get("total_input_sats"),
        "total_output_sats": record.get("total_output_sats"),
        "repeated_output_denominations": record.get("repeated_output_denominations", {}),
        "wallets": record_wallets(record),
    }


def build_divergences(transactions: JsonObject) -> dict[str, list[JsonObject]]:
    divergences: dict[str, list[JsonObject]] = {
        "missed_by_blocksci": [],
        "blocksci_only": [],
        "shared_tx_mismatches": [],
    }

    for txid, transaction in sorted(transactions.items()):
        comparison = transaction["comparison"]
        status = comparison["status"]
        coinjoin_analysis_record = transaction.get("coinjoin_analysis")
        blocksci_record = transaction.get("blocksci")
        coinjoin_analysis_summary = record_summary(coinjoin_analysis_record)
        blocksci_summary = record_summary(blocksci_record)

        if status == "missed_by_blocksci":
            divergences["missed_by_blocksci"].append(
                {
                    "txid": txid,
                    "reason": "coinjoin-analysis reported CoinJoin, BlockSci did not detect it",
                    "coinjoin_analysis": coinjoin_analysis_summary,
                    "blocksci": None,
                }
            )
        elif status == "blocksci_only":
            divergences["blocksci_only"].append(
                {
                    "txid": txid,
                    "reason": "BlockSci detected CoinJoin, coinjoin-analysis did not report it",
                    "coinjoin_analysis": None,
                    "blocksci": blocksci_summary,
                }
            )
        elif comparison["field_mismatches"]:
            divergences["shared_tx_mismatches"].append(
                {
                    "txid": txid,
                    "reason": "both sources contain the transaction, but normalized fields differ",
                    "mismatch_count": len(comparison["field_mismatches"]),
                    "mismatches": comparison["field_mismatches"],
                    "coinjoin_analysis": coinjoin_analysis_summary,
                    "blocksci": blocksci_summary,
                }
            )

    return divergences


def build_detection_confusion_matrix(
    emulator_data: JsonObject | None,
    detected_records: Mapping[str, object],
) -> DetectionConfusionMatrix | None:
    if not emulator_data or not (emulator_data.get("label_provenance") or {}).get("independent"):
        return None

    emulator_transactions = emulator_data.get("transactions") or {}
    evaluated_txids = set(emulator_transactions)
    detected_txids = set(detected_records)
    detected_in_scope_txids = detected_txids & evaluated_txids
    out_of_scope_detected_txids = sorted(detected_txids - evaluated_txids)
    true_positive = false_positive = true_negative = false_negative = unknown = 0
    false_positives = []
    false_negatives = []

    for txid, tx in emulator_transactions.items():
        expected = tx.get("is_coinjoin")
        detected = txid in detected_in_scope_txids
        if expected is True and detected:
            true_positive += 1
        elif expected is True:
            false_negative += 1
            false_negatives.append(txid)
        elif expected is False and detected:
            false_positive += 1
            false_positives.append(txid)
        elif expected is False:
            true_negative += 1
        else:
            unknown += 1

    precision = compute_optional_rate(true_positive, true_positive + false_positive)
    recall = compute_optional_rate(true_positive, true_positive + false_negative)
    specificity = compute_optional_rate(true_negative, true_negative + false_positive)
    false_positive_rate = compute_optional_rate(false_positive, false_positive + true_negative)

    return {
        "evaluated_transactions": len(evaluated_txids),
        "detected_transactions": len(detected_txids),
        "detected_in_scope": len(detected_in_scope_txids),
        "out_of_scope_detected": len(out_of_scope_detected_txids),
        "true_positives": true_positive,
        "false_positives": false_positive,
        "true_negatives": true_negative,
        "false_negatives": false_negative,
        "unknown": unknown,
        "precision": precision,
        "recall": recall,
        "f1": f1_score(precision, recall),
        "specificity": specificity,
        "false_positive_rate": false_positive_rate,
        "false_positive_txids": sorted(false_positives),
        "false_negative_txids": sorted(false_negatives),
        "out_of_scope_detected_txids": out_of_scope_detected_txids,
    }
