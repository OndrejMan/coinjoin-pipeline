"""Build emulator-side labels and transaction records from exported regtest blocks."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from exporters.artifact_paths import coinjoin_analysis_dir, emulator_dir
from exporters.common import (
    EMULATOR_DATA_SCHEMA_VERSION,
    JsonObject,
    JsonValue,
    coerce_sats,
    file_sha256,
    io_items,
    load_json,
    to_json_text,
)
from exporters.normalization import is_coinbase_tx, iter_exported_blocks, output_address

WASABI_BROADCAST_RE = re.compile(
    r"successfully\s+broadcast(?:ed)?\s+(?:the\s+)?coinjoin(?:\s+transaction)?:\s*"
    r"(?P<txid>[0-9a-f]{64})",
    re.IGNORECASE,
)
WASABI_ROUND_ID_RE = re.compile(r"\bRound\s+\((?P<round_id>[^)]+)\):", re.IGNORECASE)
PRODUCER_LABEL_MANIFEST = "coinjoin_label_manifest.json"
PRODUCER_LABEL_MANIFEST_SCHEMA_VERSION = "1.0"
WASABI_PARSEABILITY_MIN_INPUTS = 5
# The only producer source each engine may declare: the JoinMarket round-event
# file by its exact path, Wasabi coordinator logs by file name.
JOINMARKET_LABEL_SOURCE = "joinmarket_round_events.json"
WASABI_LABEL_SOURCE_NAME = "Logs.txt"
SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def _label_provenance(
    *,
    independent: bool,
    sources: list[str] | None = None,
    positive_rule: str | None = None,
    manifest: str | None = None,
    manifest_schema_version: str | None = None,
    producer_positive_count: int | None = None,
    unavailable_reason: str | None = None,
) -> JsonObject:
    return {
        "independent": independent,
        "sources": sources or [],
        "positive_rule": positive_rule,
        "baseline_used_for_labels": False,
        "manifest": manifest,
        "manifest_schema_version": manifest_schema_version,
        "producer_positive_count": producer_positive_count,
        "unavailable_reason": unavailable_reason,
    }


def _verify_label_source(data_dir: Path, record: JsonValue, expected_engine: str) -> tuple[Path | None, str]:
    """Resolve one manifest source record: its path and run-relative name, or None and why not."""
    if not isinstance(record, dict):
        return None, "producer label manifest contains an invalid source record"
    relative_value = record.get("path")
    if not isinstance(relative_value, str) or not relative_value:
        return None, "producer label manifest source path is missing"
    source_path = (data_dir / relative_value).resolve()
    try:
        source_path.relative_to(data_dir.resolve())
    except ValueError:
        return None, "producer label manifest source escapes the data directory"
    if expected_engine == "joinmarket" and relative_value != JOINMARKET_LABEL_SOURCE:
        return None, "JoinMarket producer manifest contains an unexpected source"
    if expected_engine == "wasabi" and source_path.name != WASABI_LABEL_SOURCE_NAME:
        return None, "Wasabi producer manifest contains an unexpected source"
    name = f"data/{relative_value}"
    if not source_path.is_file():
        return None, f"producer label source is missing: {name}"
    expected_size = record.get("size_bytes")
    expected_sha256 = record.get("sha256")
    if (
        not isinstance(expected_size, int)
        or expected_size < 0
        or not isinstance(expected_sha256, str)
        or not SHA256_HEX_RE.fullmatch(expected_sha256)
    ):
        return None, "producer label manifest source metadata is invalid"
    if source_path.stat().st_size != expected_size:
        return None, f"producer label source size does not match manifest: {name}"
    if file_sha256(source_path) != expected_sha256:
        return None, f"producer label source hash does not match manifest: {name}"
    return source_path, name


def verified_producer_label_sources(
    run_dir: Path,
    coinjoin_type: str,
) -> tuple[list[Path], JsonObject]:
    """Return complete, hash-verified producer sources or fail closed."""

    expected_engine = {"joinmarket": "joinmarket", "wasabi2": "wasabi"}.get(coinjoin_type)
    if expected_engine is None:
        return [], _label_provenance(
            independent=False,
            unavailable_reason=f"unsupported coinjoin type: {coinjoin_type}",
        )

    data_dir = run_dir / "data"
    manifest_path = data_dir / PRODUCER_LABEL_MANIFEST
    manifest_source = str(manifest_path.relative_to(run_dir))
    if not manifest_path.is_file():
        return [], _label_provenance(
            independent=False,
            unavailable_reason=f"producer label manifest is missing: {manifest_source}",
        )

    try:
        manifest = load_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [], _label_provenance(
            independent=False,
            manifest=manifest_source,
            unavailable_reason=f"producer label manifest cannot be read: {error}",
        )

    raw_positive_rule = manifest.get("positive_rule")
    positive_rule = str(raw_positive_rule) if raw_positive_rule else None
    producer_positive_count = manifest.get("positive_count")
    manifest_schema_version = to_json_text(manifest.get("schema_version"))

    def rejected(reason: str, positive_count: int | None = None) -> tuple[list[Path], JsonObject]:
        return [], _label_provenance(
            independent=False,
            manifest=manifest_source,
            manifest_schema_version=manifest_schema_version,
            positive_rule=positive_rule,
            producer_positive_count=positive_count,
            unavailable_reason=reason,
        )

    if manifest.get("schema_version") != PRODUCER_LABEL_MANIFEST_SCHEMA_VERSION:
        return rejected("unsupported producer label manifest schema version")
    if manifest.get("engine") != expected_engine:
        return rejected("producer label manifest engine does not match the run")
    if manifest.get("complete") is not True:
        return rejected(str(manifest.get("reason") or "producer labels are incomplete"))
    if producer_positive_count is not None and (
        isinstance(producer_positive_count, bool)
        or not isinstance(producer_positive_count, int)
        or producer_positive_count < 0
    ):
        return rejected("producer label manifest positive count is invalid")

    source_records = manifest.get("sources")
    if not isinstance(source_records, list) or not source_records:
        return rejected("complete producer label manifest has no sources", producer_positive_count)

    source_paths: list[Path] = []
    source_names: list[str] = []
    for record in source_records:
        source_path, name_or_reason = _verify_label_source(data_dir, record, expected_engine)
        if source_path is None:
            return rejected(name_or_reason)
        source_paths.append(source_path)
        source_names.append(name_or_reason)

    return source_paths, _label_provenance(
        independent=True,
        sources=source_names,
        positive_rule=positive_rule,
        manifest=manifest_source,
        manifest_schema_version=manifest_schema_version,
        producer_positive_count=producer_positive_count,
    )


def load_wallet_address_mapping(run_dir: Path, coinjoin_analysis_data: JsonObject) -> dict[str, str]:
    mapping = {
        str(address): str(wallet)
        for address, wallet in (coinjoin_analysis_data.get("address_wallet_mapping") or {}).items()
        if address and wallet
    }

    for tx in (coinjoin_analysis_data.get("coinjoins") or {}).values():
        for side in ("inputs", "outputs"):
            for record in (tx.get(side) or {}).values():
                address = record.get("address") if isinstance(record, dict) else None
                record_wallet_name = record.get("wallet_name") if isinstance(record, dict) else None
                if address and record_wallet_name:
                    mapping.setdefault(str(address), str(record_wallet_name))

    wallets_info = coinjoin_analysis_data.get("wallets_info")
    if not wallets_info:
        wallets_info_path = run_dir / "wallets_info.json"
        if wallets_info_path.exists():
            wallets_info = load_json(wallets_info_path)

    if isinstance(wallets_info, dict):
        for info_wallet_name, wallet_addresses in wallets_info.items():
            if isinstance(wallet_addresses, dict):
                for address in wallet_addresses:
                    if address:
                        mapping.setdefault(str(address), str(info_wallet_name))
            elif isinstance(wallet_addresses, list):
                for record in wallet_addresses:
                    if isinstance(record, dict) and record.get("address"):
                        mapping.setdefault(str(record["address"]), str(info_wallet_name))

    wallets_coins = coinjoin_analysis_data.get("wallets_coins")
    if not wallets_coins:
        wallets_coins_path = run_dir / "wallets_coins.json"
        if wallets_coins_path.exists():
            wallets_coins = load_json(wallets_coins_path)

    if isinstance(wallets_coins, dict):
        for coins_wallet_name, coins in wallets_coins.items():
            if not isinstance(coins, list):
                continue
            for coin in coins:
                if isinstance(coin, dict) and coin.get("address"):
                    mapping.setdefault(str(coin["address"]), str(coins_wallet_name))

    return mapping


def normalize_emulator_io_record(
    index: int,
    value: JsonValue,
    address: str | None,
    wallet_mapping: dict[str, str],
    extra: JsonObject | None = None,
    *,
    value_unit: str = "sats",
) -> JsonObject:
    record = {
        "index": str(index),
        "value": coerce_sats(value, unit=value_unit),
        "address": address,
        "wallet_name": wallet_mapping.get(address) if address else None,
        "label_source": "wallet_ownership" if address and address in wallet_mapping else "unknown",
    }
    if extra:
        record.update(extra)
    return record


def _mark_transaction_labels_unavailable(
    transactions: JsonObject,
    label_provenance: JsonObject,
    reason: str,
) -> None:
    label_provenance["independent"] = False
    label_provenance["unavailable_reason"] = reason
    for record in transactions.values():
        record["is_coinjoin"] = None
        record["protocol"] = None
        record["label_source"] = "unknown"


def load_producer_labels(
    raw_emulator_dir: Path,
    coinjoin_type: str,
    label_source_paths: list[Path],
    label_provenance: JsonObject,
) -> dict[str, JsonObject]:
    """Producer-positive labels by txid; a parse failure or count mismatch revokes independence."""
    if label_provenance["independent"] is not True:
        return {}
    try:
        if coinjoin_type == "joinmarket":
            labels = {
                str(label["txid"]): label
                for label in load_joinmarket_round_labels(label_source_paths[0])
                if label.get("txid") and label.get("status") == "confirmed"
            }
        elif coinjoin_type == "wasabi2":
            labels = {
                str(label["txid"]): label
                for label in load_wasabi_round_labels(raw_emulator_dir, label_source_paths)
                if label.get("txid")
            }
        else:
            labels = {}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        label_provenance["independent"] = False
        label_provenance["unavailable_reason"] = f"producer label source cannot be parsed: {error}"
        return {}
    expected = label_provenance.get("producer_positive_count")
    if isinstance(expected, int) and len(labels) != expected:
        label_provenance["independent"] = False
        label_provenance["unavailable_reason"] = (
            "parsed producer-positive transaction count does not match manifest: "
            f"parsed {len(labels)}, expected {expected}"
        )
    return labels


def exported_transactions(run_dir: Path, wallet_mapping: dict[str, str]) -> JsonObject:
    """Every exported non-coinbase transaction, its inputs resolved against earlier outputs."""
    output_index: dict[tuple[str, str], JsonObject] = {}
    transactions: JsonObject = {}
    for _path, height, block in iter_exported_blocks(run_dir):
        for tx in block.get("tx", []):
            txid = to_json_text(tx.get("txid"))
            if not txid:
                continue

            for output in tx.get("vout", []):
                output_n = output.get("n")
                if output_n is None:
                    continue
                output_index[(txid, str(output_n))] = {
                    "address": output_address(output),
                    # Bitcoin Core block JSON encodes vout amounts in BTC.
                    "value": coerce_sats(output.get("value"), unit="btc"),
                }

            if is_coinbase_tx(tx):
                continue

            inputs = []
            for index, input_value in enumerate(tx.get("vin", [])):
                prev_txid = to_json_text(input_value.get("txid"))
                prev_vout = input_value.get("vout")
                prevout = (
                    output_index.get((prev_txid, str(prev_vout)))
                    if prev_txid is not None and prev_vout is not None
                    else None
                )
                inputs.append(
                    normalize_emulator_io_record(
                        index,
                        prevout.get("value") if prevout else None,
                        prevout.get("address") if prevout else None,
                        wallet_mapping,
                        {
                            "prev_txid": prev_txid,
                            "prev_vout": str(prev_vout) if prev_vout is not None else None,
                        },
                    )
                )

            outputs = [
                normalize_emulator_io_record(
                    int(output.get("n", index)),
                    output.get("value"),
                    output_address(output),
                    wallet_mapping,
                    # Raw block vout amounts are BTC; prevout inputs above
                    # already come through output_index as integer sats.
                    value_unit="btc",
                )
                for index, output in enumerate(tx.get("vout", []))
            ]
            transactions[txid] = {"txid": txid, "block_height": height, "inputs": inputs, "outputs": outputs}
    return transactions


def label_transaction(
    record: JsonObject,
    label: JsonObject | None,
    coinjoin_type: str,
    independent_labels_available: bool,
) -> None:
    """Add the ground-truth label and wallet participation to an exported transaction."""
    input_wallets = sorted({item["wallet_name"] for item in record["inputs"] if item.get("wallet_name")})
    output_wallets = sorted({item["wallet_name"] for item in record["outputs"] if item.get("wallet_name")})
    is_coinjoin = label is not None if independent_labels_available else None
    record.update(
        {
            "is_coinjoin": is_coinjoin,
            "protocol": coinjoin_type if is_coinjoin else None,
            "round_id": to_json_text(label.get("round_id")) if label else None,
            "participant_wallets": sorted(set(input_wallets) | set(output_wallets)),
            "input_wallets": input_wallets,
            "output_wallets": output_wallets,
            "label_source": (
                f"emulator_{coinjoin_type}_producer"
                if is_coinjoin
                else "emulator_producer_absence"
                if independent_labels_available
                else "unknown"
            ),
        }
    )
    if coinjoin_type == "joinmarket":
        record["input_owners"] = input_wallets
        record["output_owners"] = output_wallets
        if label is not None:
            record["joinmarket_round_label"] = label
            record["taker"] = label.get("taker")
            record["candidate_makers"] = label.get("candidate_makers", [])
            record["label_source"] = "emulator_joinmarket_round"
    elif coinjoin_type == "wasabi2" and label is not None:
        record["wasabi_round_label"] = label
        record["label_source"] = "emulator_wasabi_coordinator_broadcast"


def build_emulator_data(
    run_dir: Path,
    coinjoin_analysis_data: JsonObject,
    coinjoin_type: str,
) -> JsonObject:
    raw_emulator_dir = emulator_dir(run_dir)
    wallet_mapping = load_wallet_address_mapping(coinjoin_analysis_dir(run_dir), coinjoin_analysis_data)
    label_source_paths, label_provenance = verified_producer_label_sources(raw_emulator_dir, coinjoin_type)
    labels_by_txid = load_producer_labels(raw_emulator_dir, coinjoin_type, label_source_paths, label_provenance)
    producer_positive_count = label_provenance.get("producer_positive_count")
    independent_labels_available = label_provenance["independent"] is True

    transactions = exported_transactions(run_dir, wallet_mapping)
    wasabi_parseability_candidate_txids = (
        sorted(txid for txid, record in transactions.items() if len(record["inputs"]) >= WASABI_PARSEABILITY_MIN_INPUTS)
        if coinjoin_type == "wasabi2"
        else []
    )
    for txid, record in transactions.items():
        label_transaction(record, labels_by_txid.get(txid), coinjoin_type, independent_labels_available)
    matched_positive_txids = {txid for txid, record in transactions.items() if record["is_coinjoin"]}

    unmatched_positive_txids = sorted(set(labels_by_txid) - matched_positive_txids)
    if (
        independent_labels_available
        and coinjoin_type == "wasabi2"
        and producer_positive_count is None
        and not labels_by_txid
        and wasabi_parseability_candidate_txids
    ):
        _mark_transaction_labels_unavailable(
            transactions,
            label_provenance,
            "complete Wasabi producer logs contained no parseable broadcast records "
            f"while exported transactions had at least {WASABI_PARSEABILITY_MIN_INPUTS} inputs: "
            + ", ".join(wasabi_parseability_candidate_txids),
        )
    elif independent_labels_available and unmatched_positive_txids:
        _mark_transaction_labels_unavailable(
            transactions,
            label_provenance,
            "producer-positive transactions are missing from the exported block set: "
            + ", ".join(unmatched_positive_txids),
        )

    label_counts = Counter(tx.get("is_coinjoin") for tx in transactions.values())
    return {
        "schema_version": EMULATOR_DATA_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "coinjoin_type": coinjoin_type,
        "label_provenance": label_provenance,
        "summary": {
            "transactions": len(transactions),
            "coinjoin_transactions": label_counts[True],
            "non_coinjoin_transactions": label_counts[False],
            "unknown_transactions": label_counts[None],
            "wallet_addresses": len(wallet_mapping),
            "labeled_io_records": sum(
                1 for tx in transactions.values() for item in io_items(tx) if item.get("wallet_name")
            ),
            "total_io_records": sum(1 for tx in transactions.values() for _item in io_items(tx)),
            "producer_positive_labels": len(labels_by_txid),
            "unmatched_positive_txids": unmatched_positive_txids,
            "wasabi_parseability_candidate_txids": wasabi_parseability_candidate_txids,
        },
        "transactions": {txid: transactions[txid] for txid in sorted(transactions)},
    }


def load_joinmarket_round_labels(path: Path) -> list[JsonObject]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError(f"JoinMarket round labels must be a JSON list of objects: {path}")

    # JoinMarket artifacts keep reconciliation details in
    # ``destination_matches``. A positive is only the sole match of a
    # confirmed event; flatten that txid for the existing label consumer.
    labels: list[JsonObject] = []
    for item in data:
        if item.get("status") in ("multiple_matches", "duplicate_destination"):
            raise ValueError(
                f"JoinMarket round {item.get('export_round_id', '?')} has destination conflict status: {item['status']}"
            )
        label = dict(item)
        matches = label.get("destination_matches")
        label.pop("txid", None)
        if (
            label.get("status") == "confirmed"
            and isinstance(matches, list)
            and len(matches) == 1
            and isinstance(matches[0], dict)
            and matches[0].get("txid")
        ):
            label["txid"] = str(matches[0]["txid"])
            if matches[0].get("block_height") is not None:
                label["block_height"] = matches[0]["block_height"]
        labels.append(label)
    return labels


def load_wasabi_round_labels(run_dir: Path, log_paths: list[Path]) -> list[JsonObject]:
    labels_by_txid: dict[str, JsonObject] = {}
    for path in log_paths:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                text = line.rstrip("\n")
                match = WASABI_BROADCAST_RE.search(text)
                if match is None:
                    continue
                round_match = WASABI_ROUND_ID_RE.search(text)
                txid = match.group("txid").lower()
                labels_by_txid[txid] = {
                    "timestamp": text[: round_match.start()].strip() if round_match else None,
                    "round_id": round_match.group("round_id") if round_match else None,
                    "txid": txid,
                    "source_file": str(path.relative_to(run_dir)),
                }
    return [labels_by_txid[txid] for txid in sorted(labels_by_txid)]


def wallet_address_labels(emulator_data: JsonObject | None) -> dict[str, str]:
    if not emulator_data:
        return {}
    labels = {}
    for tx in (emulator_data.get("transactions") or {}).values():
        for item in io_items(tx):
            address = item.get("address")
            item_wallet_name = item.get("wallet_name")
            if address and item_wallet_name:
                labels[str(address)] = str(item_wallet_name)
    return labels
