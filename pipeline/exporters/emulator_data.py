"""Build emulator-side labels and transaction records from exported regtest blocks."""

from __future__ import annotations

import json
from pathlib import Path

from exporters.artifact_paths import coinjoin_analysis_dir, emulator_dir
from exporters.common import EMULATOR_DATA_SCHEMA_VERSION, JsonObject, JsonValue, coerce_sats, load_json, to_json_text
from exporters.normalization import block_height_from_path, is_coinbase_tx, output_address


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


def build_emulator_data(
    run_dir: Path,
    coinjoin_analysis_data: JsonObject,
    coinjoin_type: str,
) -> JsonObject:
    raw_emulator_dir = emulator_dir(run_dir)
    block_dir = raw_emulator_dir / "data" / "btc-node"
    wallet_mapping = load_wallet_address_mapping(coinjoin_analysis_dir(run_dir), coinjoin_analysis_data)
    coinjoins = coinjoin_analysis_data.get("coinjoins") or {}
    coinjoin_txids = {str(tx.get("txid") or txid) for txid, tx in coinjoins.items()}
    round_by_txid = {
        str(tx.get("txid") or txid): to_json_text(tx.get("round_id"))
        for txid, tx in coinjoins.items()
    }
    output_index: dict[tuple[str, str], JsonObject] = {}
    transactions: JsonObject = {}
    joinmarket_round_labels = load_joinmarket_round_labels(raw_emulator_dir) if coinjoin_type == "joinmarket" else []
    joinmarket_labels_by_txid = {
        str(label["txid"]): label
        for label in joinmarket_round_labels
        if label.get("txid")
    }
    joinmarket_labels_by_destination = {
        str(label["destination_address"]): label
        for label in joinmarket_round_labels
        if label.get("destination_address")
    }

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
                is_coinjoin = txid in coinjoin_txids
                tx_record = {
                    "txid": txid,
                    "block_height": height,
                    "is_coinjoin": is_coinjoin,
                    "protocol": coinjoin_type if is_coinjoin else None,
                    "round_id": round_by_txid.get(txid),
                    "participant_wallets": participant_wallets,
                    "input_wallets": input_wallets,
                    "output_wallets": output_wallets,
                    "label_source": "emulator_round" if is_coinjoin else "wallet_ownership",
                    "inputs": inputs,
                    "outputs": outputs,
                }
                if coinjoin_type == "joinmarket":
                    tx_record["input_owners"] = input_wallets
                    tx_record["output_owners"] = output_wallets
                    label = joinmarket_labels_by_txid.get(txid)
                    if label is None:
                        for output in outputs:
                            label = joinmarket_labels_by_destination.get(str(output.get("address")))
                            if label is not None:
                                break
                    if label is not None:
                        tx_record["joinmarket_round_label"] = label
                        tx_record["round_id"] = tx_record.get("round_id") or to_json_text(label.get("round_id"))
                        tx_record["taker"] = label.get("taker")
                        tx_record["candidate_makers"] = label.get("candidate_makers", [])
                        tx_record["label_source"] = (
                            "emulator_joinmarket_round"
                            if is_coinjoin
                            else "emulator_joinmarket_round_candidate"
                        )
                transactions[txid] = tx_record

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
        "summary": {
            "transactions": len(transactions),
            "coinjoin_transactions": true_count,
            "non_coinjoin_transactions": false_count,
            "unknown_transactions": unknown_count,
            "wallet_addresses": len(wallet_mapping),
            "labeled_io_records": labeled_io,
            "total_io_records": total_io,
        },
        "transactions": {
            txid: transactions[txid]
            for txid in sorted(transactions)
        },
    }


def load_joinmarket_round_labels(run_dir: Path) -> list[JsonObject]:
    candidates = [
        run_dir / "data" / "joinmarket_round_events.json",
        run_dir / "joinmarket_round_events.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


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
