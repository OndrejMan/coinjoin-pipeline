"""Comparison, divergence, detection, and clustering metrics."""

from __future__ import annotations

from exporters.common import JsonObject


def compare_io(
    label: str,
    coinjoin_analysis_values: list[JsonObject],
    blocksci_values: list[JsonObject],
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


def compare_records(coinjoin_analysis_record: JsonObject, blocksci_record: JsonObject) -> list[str]:
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


def record_wallets(record: JsonObject | None) -> list[str]:
    if record is None:
        return []

    wallets = set()
    for side in ("inputs", "outputs"):
        for item in record.get(side, []):
            wallet = item.get("wallet_name")
            if wallet:
                wallets.add(wallet)
    return sorted(wallets)


def record_summary(record: JsonObject | None) -> JsonObject | None:
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


def compute_rate(numerator: int, denominator: int, empty_default: float) -> float:
    if denominator == 0:
        return empty_default
    return round(numerator / denominator, 6)


def compute_optional_rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def build_detection_confusion_matrix(
    emulator_data: JsonObject | None,
    blocksci_records: dict[str, JsonObject],
) -> JsonObject | None:
    if not emulator_data:
        return None

    detected_txids = set(blocksci_records)
    true_positive = false_positive = true_negative = false_negative = unknown = 0
    false_positives = []
    false_negatives = []

    for txid, tx in (emulator_data.get("transactions") or {}).items():
        expected = tx.get("is_coinjoin")
        detected = txid in detected_txids
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
    f1 = None
    if precision is not None and recall is not None and precision + recall > 0:
        f1 = round(2 * precision * recall / (precision + recall), 6)

    return {
        "true_positives": true_positive,
        "false_positives": false_positive,
        "true_negatives": true_negative,
        "false_negatives": false_negative,
        "unknown": unknown,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "specificity": specificity,
        "false_positive_rate": false_positive_rate,
        "false_positive_txids": sorted(false_positives),
        "false_negative_txids": sorted(false_negatives),
    }


def wallet_address_labels(emulator_data: JsonObject | None) -> dict[str, str]:
    if not emulator_data:
        return {}
    labels = {}
    for tx in (emulator_data.get("transactions") or {}).values():
        for side in ("inputs", "outputs"):
            for item in tx.get(side, []):
                address = item.get("address")
                item_wallet_name = item.get("wallet_name")
                if address and item_wallet_name:
                    labels[str(address)] = str(item_wallet_name)
    return labels


def pairs_by_partition(labels_by_item: dict[str, str]) -> set[tuple[str, str]]:
    grouped: dict[str, list[str]] = {}
    for item, label in labels_by_item.items():
        grouped.setdefault(label, []).append(item)

    pairs = set()
    for items in grouped.values():
        sorted_group_items = sorted(items)
        for left_index, left in enumerate(sorted_group_items):
            for right in sorted_group_items[left_index + 1:]:
                pairs.add((left, right))
    return pairs


def evaluate_cluster_assignments(
    emulator_data: JsonObject | None,
    predicted_address_clusters: dict[str, str] | None = None,
    unavailable_reason: str | None = None,
) -> JsonObject:
    labels_by_address = wallet_address_labels(emulator_data)
    if not predicted_address_clusters:
        return {
            "available": False,
            "reason": unavailable_reason or "BlockSci cluster assignments were not exported for this run.",
            "labeled_addresses": len(labels_by_address),
            "clustered_labeled_addresses": 0,
            "unclustered_labeled_addresses": len(labels_by_address),
        }

    comparable_addresses = sorted(set(labels_by_address) & set(predicted_address_clusters))
    truth_pairs = pairs_by_partition({address: labels_by_address[address] for address in comparable_addresses})
    predicted_pairs = pairs_by_partition(
        {address: str(predicted_address_clusters[address]) for address in comparable_addresses}
    )
    true_positive_pairs = len(truth_pairs & predicted_pairs)
    false_positive_pairs = len(predicted_pairs - truth_pairs)
    false_negative_pairs = len(truth_pairs - predicted_pairs)
    precision = compute_optional_rate(true_positive_pairs, true_positive_pairs + false_positive_pairs)
    recall = compute_optional_rate(true_positive_pairs, true_positive_pairs + false_negative_pairs)
    f1 = None
    if precision is not None and recall is not None and precision + recall > 0:
        f1 = round(2 * precision * recall / (precision + recall), 6)

    wallets_by_cluster: dict[str, set[str]] = {}
    clusters_by_wallet: dict[str, set[str]] = {}
    for address in comparable_addresses:
        wallet = labels_by_address[address]
        cluster = str(predicted_address_clusters[address])
        wallets_by_cluster.setdefault(cluster, set()).add(wallet)
        clusters_by_wallet.setdefault(wallet, set()).add(cluster)

    overmerged = {
        cluster: sorted(wallets)
        for cluster, wallets in wallets_by_cluster.items()
        if len(wallets) > 1
    }
    undermerged = {
        wallet: sorted(clusters)
        for wallet, clusters in clusters_by_wallet.items()
        if len(clusters) > 1
    }

    return {
        "available": True,
        "labeled_addresses": len(labels_by_address),
        "clustered_labeled_addresses": len(comparable_addresses),
        "unclustered_labeled_addresses": len(set(labels_by_address) - set(comparable_addresses)),
        "pairwise_true_positives": true_positive_pairs,
        "pairwise_false_positives": false_positive_pairs,
        "pairwise_false_negatives": false_negative_pairs,
        "pairwise_precision": precision,
        "pairwise_recall": recall,
        "pairwise_f1": f1,
        "overmerged_clusters": len(overmerged),
        "undermerged_wallets": len(undermerged),
        "largest_overmerged_clusters": [
            {"cluster": cluster, "wallets": wallets, "wallet_count": len(wallets)}
            for cluster, wallets in sorted(overmerged.items(), key=lambda item: (-len(item[1]), item[0]))[:10]
        ],
        "largest_undermerged_wallets": [
            {"wallet": wallet, "clusters": clusters, "cluster_count": len(clusters)}
            for wallet, clusters in sorted(undermerged.items(), key=lambda item: (-len(item[1]), item[0]))[:10]
        ],
    }
