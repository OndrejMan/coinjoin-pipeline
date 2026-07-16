"""Build emulator-side labels and transaction records from exported regtest blocks."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from exporters.artifact_paths import coinjoin_analysis_dir, emulator_dir
from exporters.common import (
    EMULATOR_DATA_SCHEMA_VERSION,
    JsonObject,
    JsonValue,
    coerce_sats,
    load_json,
    to_json_text,
)
from exporters.normalization import block_height_from_path, is_coinbase_tx, output_address

WASABI_BROADCAST_RE = re.compile(
    r"successfully\s+broadcast(?:ed)?\s+(?:the\s+)?coinjoin(?:\s+transaction)?:\s*"
    r"(?P<txid>[0-9a-f]{64})",
    re.IGNORECASE,
)
WASABI_ROUND_ID_RE = re.compile(r"\bRound\s+\((?P<round_id>[^)]+)\):", re.IGNORECASE)
PRODUCER_LABEL_MANIFEST = "coinjoin_label_manifest.json"
PRODUCER_LABEL_MANIFEST_SCHEMA_VERSION = "1.0"
WASABI_PARSEABILITY_MIN_INPUTS = 5


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            manifest=None,
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

    positive_rule = manifest.get("positive_rule")
    producer_positive_count = manifest.get("positive_count")
    manifest_schema_version = to_json_text(manifest.get("schema_version"))
    if manifest.get("schema_version") != PRODUCER_LABEL_MANIFEST_SCHEMA_VERSION:
        return [], _label_provenance(
            independent=False,
            manifest=manifest_source,
            manifest_schema_version=manifest_schema_version,
            positive_rule=str(positive_rule) if positive_rule else None,
            unavailable_reason="unsupported producer label manifest schema version",
        )
    if manifest.get("engine") != expected_engine:
        return [], _label_provenance(
            independent=False,
            manifest=manifest_source,
            manifest_schema_version=manifest_schema_version,
            positive_rule=str(positive_rule) if positive_rule else None,
            unavailable_reason="producer label manifest engine does not match the run",
        )
    if manifest.get("complete") is not True:
        return [], _label_provenance(
            independent=False,
            manifest=manifest_source,
            manifest_schema_version=manifest_schema_version,
            positive_rule=str(positive_rule) if positive_rule else None,
            unavailable_reason=str(manifest.get("reason") or "producer labels are incomplete"),
        )
    if producer_positive_count is not None and (
        isinstance(producer_positive_count, bool)
        or not isinstance(producer_positive_count, int)
        or producer_positive_count < 0
    ):
        return [], _label_provenance(
            independent=False,
            manifest=manifest_source,
            manifest_schema_version=manifest_schema_version,
            positive_rule=str(positive_rule) if positive_rule else None,
            unavailable_reason="producer label manifest positive count is invalid",
        )

    source_records = manifest.get("sources")
    if not isinstance(source_records, list) or not source_records:
        return [], _label_provenance(
            independent=False,
            manifest=manifest_source,
            manifest_schema_version=manifest_schema_version,
            producer_positive_count=producer_positive_count,
            positive_rule=str(positive_rule) if positive_rule else None,
            unavailable_reason="complete producer label manifest has no sources",
        )

    data_root = data_dir.resolve()
    source_paths: list[Path] = []
    source_names: list[str] = []
    for record in source_records:
        if not isinstance(record, dict):
            reason = "producer label manifest contains an invalid source record"
            break
        relative_value = record.get("path")
        if not isinstance(relative_value, str) or not relative_value:
            reason = "producer label manifest source path is missing"
            break
        source_path = (data_dir / relative_value).resolve()
        try:
            source_path.relative_to(data_root)
        except ValueError:
            reason = "producer label manifest source escapes the data directory"
            break
        if expected_engine == "joinmarket" and relative_value != "joinmarket_round_events.json":
            reason = "JoinMarket producer manifest contains an unexpected source"
            break
        if expected_engine == "wasabi" and source_path.name != "Logs.txt":
            reason = "Wasabi producer manifest contains an unexpected source"
            break
        if not source_path.is_file():
            reason = f"producer label source is missing: data/{relative_value}"
            break
        expected_size = record.get("size_bytes")
        expected_sha256 = record.get("sha256")
        if (
            not isinstance(expected_size, int)
            or expected_size < 0
            or not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        ):
            reason = "producer label manifest source metadata is invalid"
            break
        if source_path.stat().st_size != expected_size:
            reason = f"producer label source size does not match manifest: data/{relative_value}"
            break
        if _sha256_file(source_path) != expected_sha256:
            reason = f"producer label source hash does not match manifest: data/{relative_value}"
            break
        source_paths.append(source_path)
        source_names.append(f"data/{relative_value}")
    else:
        return source_paths, _label_provenance(
            independent=True,
            sources=source_names,
            positive_rule=str(positive_rule) if positive_rule else None,
            manifest=manifest_source,
            manifest_schema_version=manifest_schema_version,
            producer_positive_count=producer_positive_count,
        )

    return [], _label_provenance(
        independent=False,
        manifest=manifest_source,
        manifest_schema_version=manifest_schema_version,
        positive_rule=str(positive_rule) if positive_rule else None,
        unavailable_reason=reason,
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
) -> JsonObject:
    record = {
        "index": str(index),
        "value": coerce_sats(value),
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


def build_emulator_data(
    run_dir: Path,
    coinjoin_analysis_data: JsonObject,
    coinjoin_type: str,
) -> JsonObject:
    raw_emulator_dir = emulator_dir(run_dir)
    block_dir = raw_emulator_dir / "data" / "btc-node"
    wallet_mapping = load_wallet_address_mapping(coinjoin_analysis_dir(run_dir), coinjoin_analysis_data)
    output_index: dict[tuple[str, str], JsonObject] = {}
    transactions: JsonObject = {}
    label_source_paths, label_provenance = verified_producer_label_sources(
        raw_emulator_dir,
        coinjoin_type,
    )
    independent_labels_available = label_provenance["independent"] is True
    joinmarket_round_labels: list[JsonObject] = []
    wasabi_round_labels: list[JsonObject] = []
    if independent_labels_available:
        try:
            if coinjoin_type == "joinmarket":
                joinmarket_round_labels = load_joinmarket_round_labels(label_source_paths[0])
            elif coinjoin_type == "wasabi2":
                wasabi_round_labels = load_wasabi_round_labels(
                    raw_emulator_dir,
                    label_source_paths,
                )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            label_provenance["independent"] = False
            label_provenance["unavailable_reason"] = (
                f"producer label source cannot be parsed: {error}"
            )
            independent_labels_available = False
            joinmarket_round_labels = []
            wasabi_round_labels = []
    joinmarket_labels_by_txid = {
        str(label["txid"]): label
        for label in joinmarket_round_labels
        if label.get("txid") and label.get("status") == "confirmed"
    }
    wasabi_labels_by_txid = {
        str(label["txid"]): label
        for label in wasabi_round_labels
        if label.get("txid")
    }
    producer_positive_txids = set(joinmarket_labels_by_txid) | set(wasabi_labels_by_txid)
    producer_positive_count = label_provenance.get("producer_positive_count")
    if (
        independent_labels_available
        and isinstance(producer_positive_count, int)
        and len(producer_positive_txids) != producer_positive_count
    ):
        label_provenance["independent"] = False
        label_provenance["unavailable_reason"] = (
            "parsed producer-positive transaction count does not match manifest: "
            f"parsed {len(producer_positive_txids)}, expected {producer_positive_count}"
        )
        independent_labels_available = False
    matched_positive_txids: set[str] = set()
    wasabi_parseability_candidate_txids: set[str] = set()

    if block_dir.exists():
        for block_path in sorted(block_dir.glob("block_*.json"), key=lambda path: block_height_from_path(path) or -1):
            block = load_json(block_path)
            height = block.get("height")
            if height is None:
                height = block_height_from_path(block_path)

            for tx in block.get("tx", []):
                txid = to_json_text(tx.get("txid"))
                if not txid:
                    continue

                for output in tx.get("vout", []):
                    output_n = output.get("n")
                    if output_n is None:
                        continue
                    address = output_address(output)
                    output_index[(txid, str(output_n))] = {
                        "address": address,
                        "value": coerce_sats(output.get("value")),
                    }

                if is_coinbase_tx(tx):
                    continue

                if (
                    coinjoin_type == "wasabi2"
                    and len(tx.get("vin", [])) >= WASABI_PARSEABILITY_MIN_INPUTS
                ):
                    wasabi_parseability_candidate_txids.add(txid)

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

                outputs = []
                for index, output in enumerate(tx.get("vout", [])):
                    outputs.append(
                        normalize_emulator_io_record(
                            int(output.get("n", index)),
                            output.get("value"),
                            output_address(output),
                            wallet_mapping,
                        )
                    )

                input_wallets = sorted({item["wallet_name"] for item in inputs if item.get("wallet_name")})
                output_wallets = sorted({item["wallet_name"] for item in outputs if item.get("wallet_name")})
                participant_wallets = sorted(set(input_wallets) | set(output_wallets))
                label = None
                if coinjoin_type == "joinmarket":
                    label = joinmarket_labels_by_txid.get(txid)
                elif coinjoin_type == "wasabi2":
                    label = wasabi_labels_by_txid.get(txid)

                is_coinjoin = label is not None if independent_labels_available else None
                if is_coinjoin:
                    matched_positive_txids.add(txid)
                tx_record = {
                    "txid": txid,
                    "block_height": height,
                    "is_coinjoin": is_coinjoin,
                    "protocol": coinjoin_type if is_coinjoin else None,
                    "round_id": to_json_text(label.get("round_id")) if label else None,
                    "participant_wallets": participant_wallets,
                    "input_wallets": input_wallets,
                    "output_wallets": output_wallets,
                    "label_source": (
                        f"emulator_{coinjoin_type}_producer"
                        if is_coinjoin
                        else "emulator_producer_absence"
                        if independent_labels_available
                        else "unknown"
                    ),
                    "inputs": inputs,
                    "outputs": outputs,
                }
                if coinjoin_type == "joinmarket":
                    tx_record["input_owners"] = input_wallets
                    tx_record["output_owners"] = output_wallets
                    if label is not None:
                        tx_record["joinmarket_round_label"] = label
                        tx_record["taker"] = label.get("taker")
                        tx_record["candidate_makers"] = label.get("candidate_makers", [])
                        tx_record["label_source"] = "emulator_joinmarket_round"
                elif coinjoin_type == "wasabi2" and label is not None:
                    tx_record["wasabi_round_label"] = label
                    tx_record["label_source"] = "emulator_wasabi_coordinator_broadcast"
                transactions[txid] = tx_record

    unmatched_positive_txids = sorted(producer_positive_txids - matched_positive_txids)
    if (
        independent_labels_available
        and coinjoin_type == "wasabi2"
        and producer_positive_count is None
        and not wasabi_round_labels
        and wasabi_parseability_candidate_txids
    ):
        candidates = ", ".join(sorted(wasabi_parseability_candidate_txids))
        _mark_transaction_labels_unavailable(
            transactions,
            label_provenance,
            "complete Wasabi producer logs contained no parseable broadcast records "
            f"while exported transactions had at least {WASABI_PARSEABILITY_MIN_INPUTS} inputs: "
            f"{candidates}",
        )
    elif independent_labels_available and unmatched_positive_txids:
        _mark_transaction_labels_unavailable(
            transactions,
            label_provenance,
            "producer-positive transactions are missing from the exported block set: "
            + ", ".join(unmatched_positive_txids),
        )

    true_count = sum(1 for tx in transactions.values() if tx.get("is_coinjoin") is True)
    false_count = sum(1 for tx in transactions.values() if tx.get("is_coinjoin") is False)
    unknown_count = sum(1 for tx in transactions.values() if tx.get("is_coinjoin") is None)
    labeled_io = sum(
        1
        for tx in transactions.values()
        for side in ("inputs", "outputs")
        for item in tx.get(side, [])
        if item.get("wallet_name")
    )
    total_io = sum(
        1
        for tx in transactions.values()
        for side in ("inputs", "outputs")
        for item in tx.get(side, [])
    )

    return {
        "schema_version": EMULATOR_DATA_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "coinjoin_type": coinjoin_type,
        "label_provenance": label_provenance,
        "summary": {
            "transactions": len(transactions),
            "coinjoin_transactions": true_count,
            "non_coinjoin_transactions": false_count,
            "unknown_transactions": unknown_count,
            "wallet_addresses": len(wallet_mapping),
            "labeled_io_records": labeled_io,
            "total_io_records": total_io,
            "producer_positive_labels": len(producer_positive_txids),
            "unmatched_positive_txids": unmatched_positive_txids,
            "wasabi_parseability_candidate_txids": sorted(wasabi_parseability_candidate_txids),
        },
        "transactions": {
            txid: transactions[txid]
            for txid in sorted(transactions)
        },
    }


def load_joinmarket_round_labels(path: Path) -> list[JsonObject]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError(f"JoinMarket round labels must be a JSON list of objects: {path}")
    return data


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
                label = {
                    "timestamp": text[:round_match.start()].strip() if round_match else None,
                    "round_id": round_match.group("round_id") if round_match else None,
                    "txid": match.group("txid"),
                }
                label["txid"] = label["txid"].lower()
                label["source_file"] = str(path.relative_to(run_dir))
                labels_by_txid[label["txid"]] = label
    return [labels_by_txid[txid] for txid in sorted(labels_by_txid)]


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
