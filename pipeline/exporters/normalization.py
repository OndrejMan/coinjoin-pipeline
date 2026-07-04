"""coinjoin-analysis and exported block normalization helpers."""

from __future__ import annotations

import re
from pathlib import Path

from exporters.artifact_paths import emulator_dir
from exporters.common import (
    DEFAULT_FIRST_WASABI2_BLOCK,
    JsonObject,
    add_common_metrics,
    coerce_sats,
    load_json,
    sorted_items,
    to_json_text,
)

FALSE_CJTXS_GLOB = "false_cjtxs.json*"


def load_false_positive_txids(analysis_dir: Path) -> tuple[set[str], list[JsonObject]]:
    """Load and merge coinjoin-analysis false-positive sidecars."""
    txids: set[str] = set()
    sources: list[JsonObject] = []
    for path in sorted(analysis_dir.glob(FALSE_CJTXS_GLOB)):
        if not path.is_file():
            continue
        data = load_json(path)
        source_txids: set[str] = set()
        for category, values in data.items():
            if not isinstance(values, list):
                raise ValueError(f"Expected a list at {category!r} in {path}")
            source_txids.update(str(value) for value in values)
        txids.update(source_txids)
        sources.append({"file": path.name, "txids": len(source_txids)})
    return txids, sources


def filter_coinjoin_analysis_false_positives(
    data: JsonObject,
    false_positive_txids: set[str],
) -> tuple[JsonObject, list[str]]:
    """Return a shallow copy with confirmed false positives removed from coinjoins."""
    coinjoins = data.get("coinjoins", {})
    if not isinstance(coinjoins, dict):
        raise ValueError("Expected coinjoin-analysis data to contain a 'coinjoins' object")
    removed = sorted(set(coinjoins).intersection(false_positive_txids))
    filtered = dict(data)
    filtered["coinjoins"] = {
        txid: transaction for txid, transaction in coinjoins.items() if txid not in false_positive_txids
    }
    return filtered, removed


def normalize_io_map(values: JsonObject) -> list[JsonObject]:
    records = []
    for index, record in sorted_items(values):
        normalized = {
            "index": str(index),
            "value": coerce_sats(record.get("value")),
            "address": to_json_text(record.get("address")),
        }
        if "wallet_name" in record:
            normalized["wallet_name"] = to_json_text(record.get("wallet_name"))
        if "mix_event_type" in record:
            normalized["mix_event_type"] = to_json_text(record.get("mix_event_type"))
        if "is_standard_denom" in record:
            normalized["is_standard_denom"] = bool(record.get("is_standard_denom"))
        if "txid" in record:
            normalized["prev_txid"] = to_json_text(record.get("txid"))
        if "spending_tx" in record:
            normalized["spending_tx"] = to_json_text(record.get("spending_tx"))
        if "spend_by_tx" in record:
            normalized["spend_by_tx"] = to_json_text(record.get("spend_by_tx"))
        records.append(normalized)
    return records


def normalize_coinjoin_analysis_record(txid: str, tx: JsonObject) -> JsonObject:
    record = {
        "txid": to_json_text(tx.get("txid") or txid),
        "broadcast_time": to_json_text(tx.get("broadcast_time")),
        "block_height": tx.get("block_height") or tx.get("block_index"),
        "round_id": to_json_text(tx.get("round_id")),
        "inputs": normalize_io_map(tx.get("inputs", {})),
        "outputs": normalize_io_map(tx.get("outputs", {})),
    }
    return add_common_metrics(record)


def normalize_coinjoin_analysis(data: JsonObject) -> dict[str, JsonObject]:
    coinjoins = data.get("coinjoins", {})
    return {
        txid: normalize_coinjoin_analysis_record(txid, tx)
        for txid, tx in sorted_items(coinjoins)
    }


def block_height_from_path(path: Path) -> int | None:
    match = re.fullmatch(r"block_(\d+)\.json", path.name)
    if match is None:
        return None
    return int(match.group(1))


def is_coinbase_tx(tx: JsonObject) -> bool:
    inputs = tx.get("vin", [])
    return bool(inputs) and "coinbase" in inputs[0]


def load_exported_block_tx_index(run_dir: Path) -> dict[str, int]:
    block_dir = emulator_dir(run_dir) / "data" / "btc-node"
    if not block_dir.exists():
        return {}

    tx_index: dict[str, int] = {}
    for block_path in sorted(block_dir.glob("block_*.json"), key=lambda path: block_height_from_path(path) or -1):
        block = load_json(block_path)
        height = block.get("height")
        if height is None:
            height = block_height_from_path(block_path)
        if height is None:
            continue

        for tx in block.get("tx", []):
            txid = tx.get("txid")
            if txid:
                tx_index[str(txid)] = int(height)

    return tx_index


def output_address(output: JsonObject) -> str | None:
    script_pub_key = output.get("scriptPubKey") or {}
    address = script_pub_key.get("address")
    if address:
        return str(address)
    addresses = script_pub_key.get("addresses")
    if isinstance(addresses, list) and addresses:
        return str(addresses[0])
    return None


def fill_missing_block_heights(
    records: dict[str, JsonObject],
    tx_block_heights: dict[str, int],
) -> None:
    for txid, record in records.items():
        if record.get("block_height") is not None:
            continue
        if txid not in tx_block_heights:
            continue
        record["block_height"] = tx_block_heights[txid]
        record["block_height_inferred"] = True


def load_first_wasabi2_block(config_path: Path, default: int = DEFAULT_FIRST_WASABI2_BLOCK) -> int:
    if not config_path.exists():
        return default
    config = load_json(config_path)
    value = (config.get("coinjoin") or config.get("coinjoinConfig") or {}).get("FirstWasabi2Block")
    if value is None:
        value = config.get("FirstWasabi2Block")
    return int(value) if value is not None else default
