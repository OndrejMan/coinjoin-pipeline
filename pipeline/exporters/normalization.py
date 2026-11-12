"""coinjoin-analysis and exported block normalization helpers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from exporters.artifact_paths import (
    EXPORTED_BLOCK_GLOB,
    EXPORTED_BLOCKS_DIR,
    block_height_from_path,
)
from exporters.common import (
    JsonObject,
    coerce_sats,
    load_json,
    sorted_items,
    to_json_text,
    transaction_metrics,
)
from exporters.report_types import IORecord, TransactionRecord


def normalize_io_map(values: JsonObject) -> list[IORecord]:
    records = []
    for index, record in sorted_items(values):
        normalized: IORecord = {
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


def normalize_coinjoin_analysis_record(txid: str, tx: JsonObject) -> TransactionRecord:
    inputs = normalize_io_map(tx.get("inputs", {}))
    outputs = normalize_io_map(tx.get("outputs", {}))
    return {
        "txid": to_json_text(tx.get("txid") or txid),
        "broadcast_time": to_json_text(tx.get("broadcast_time")),
        "block_height": tx.get("block_height") or tx.get("block_index"),
        "round_id": to_json_text(tx.get("round_id")),
        "inputs": inputs,
        "outputs": outputs,
        **transaction_metrics(inputs, outputs),
    }


def normalize_coinjoin_analysis(data: JsonObject) -> dict[str, TransactionRecord]:
    coinjoins = data.get("coinjoins", {})
    return {txid: normalize_coinjoin_analysis_record(txid, tx) for txid, tx in sorted_items(coinjoins)}


def is_coinbase_tx(tx: JsonObject) -> bool:
    inputs = tx.get("vin", [])
    return bool(inputs) and "coinbase" in inputs[0]


def exported_block_paths(run_dir: Path) -> list[Path]:
    """The emulator's exported ``block_<height>.json`` files, lowest height first."""
    block_dir = run_dir / EXPORTED_BLOCKS_DIR
    if not block_dir.is_dir():
        return []
    return sorted(block_dir.glob(EXPORTED_BLOCK_GLOB), key=lambda path: block_height_from_path(path) or -1)


def iter_exported_blocks(run_dir: Path) -> Iterator[tuple[Path, int | None, JsonObject]]:
    """Each exported block with its height: the block's own, else the file name's.

    Inputs reference outputs of lower blocks, so consumers that index outputs
    rely on this order.
    """
    for path in exported_block_paths(run_dir):
        block = load_json(path)
        height = block.get("height")
        if height is None:
            height = block_height_from_path(path)
        yield path, int(height) if height is not None else None, block


def load_exported_block_tx_index(run_dir: Path) -> dict[str, int]:
    tx_index: dict[str, int] = {}
    for _path, height, block in iter_exported_blocks(run_dir):
        if height is None:
            continue
        for tx in block.get("tx", []):
            txid = tx.get("txid")
            if txid:
                tx_index[str(txid)] = height
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
    records: dict[str, TransactionRecord],
    tx_block_heights: dict[str, int],
) -> None:
    for txid, record in records.items():
        if record.get("block_height") is not None:
            continue
        if txid not in tx_block_heights:
            continue
        record["block_height"] = tx_block_heights[txid]
        record["block_height_inferred"] = True


def load_first_wasabi2_block(config_path: Path) -> int:
    """The Wasabi 2 start height BlockSci's heuristic reads from its chain configuration.

    ``blocksci_parser generate-config`` writes it under ``chainConfig.coinJoinConfiguration``
    for every network (0 on regtest and testnet).
    """
    chain = load_json(config_path).get("chainConfig")
    coinjoin = chain.get("coinJoinConfiguration") if isinstance(chain, dict) else None
    value = coinjoin.get("FirstWasabi2Block") if isinstance(coinjoin, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{config_path} has no integer chainConfig.coinJoinConfiguration.FirstWasabi2Block")
    return value
