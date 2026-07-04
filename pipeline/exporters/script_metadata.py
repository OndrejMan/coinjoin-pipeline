"""Script/address metadata extraction from exported regtest blocks."""

from __future__ import annotations

from pathlib import Path

from exporters.artifact_paths import emulator_dir
from exporters.common import JsonObject, load_json, to_json_text
from exporters.normalization import block_height_from_path


def blocksci_address_type_from_script_type(script_type: str | None) -> str | None:
    if script_type == "witness_v0_keyhash":
        return "WITNESS_PUBKEYHASH"
    if script_type == "witness_v0_scripthash":
        return "WITNESS_SCRIPTHASH"
    if script_type and (script_type == "witness_v1_taproot" or script_type.startswith("witness_")):
        return "WITNESS_UNKNOWN"
    return None


def script_metadata(script_pub_key: JsonObject | None) -> JsonObject:
    if not script_pub_key:
        return {}

    script_type = to_json_text(script_pub_key.get("type"))
    metadata = {
        "script_type": script_type,
        "script_asm": to_json_text(script_pub_key.get("asm")),
        "script_hex": to_json_text(script_pub_key.get("hex")),
        "address_type": blocksci_address_type_from_script_type(script_type),
    }
    return {key: value for key, value in metadata.items() if value is not None}


def load_exported_block_script_metadata(
    run_dir: Path,
) -> tuple[dict[tuple[str, str], JsonObject], dict[tuple[str, str], tuple[str, str]]]:
    block_dir = emulator_dir(run_dir) / "data" / "btc-node"
    if not block_dir.exists():
        return {}, {}

    outputs: dict[tuple[str, str], JsonObject] = {}
    input_prevouts: dict[tuple[str, str], tuple[str, str]] = {}
    for block_path in sorted(block_dir.glob("block_*.json"), key=lambda path: block_height_from_path(path) or -1):
        block = load_json(block_path)
        for tx in block.get("tx", []):
            txid = to_json_text(tx.get("txid"))
            if not txid:
                continue

            for index, input_value in enumerate(tx.get("vin", [])):
                prev_txid = to_json_text(input_value.get("txid"))
                prev_vout = input_value.get("vout")
                if prev_txid is not None and prev_vout is not None:
                    input_prevouts[(txid, str(index))] = (prev_txid, str(prev_vout))

            for output in tx.get("vout", []):
                output_index = output.get("n")
                if output_index is None:
                    continue
                metadata = script_metadata(output.get("scriptPubKey"))
                if metadata:
                    outputs[(txid, str(output_index))] = metadata

    return outputs, input_prevouts


def apply_script_metadata(record: JsonObject, metadata: JsonObject | None) -> None:
    if not metadata:
        return
    for key in ("script_type", "script_asm", "script_hex", "address_type"):
        if key in metadata and not record.get(key):
            record[key] = metadata[key]


def enrich_records_with_script_metadata(records: dict[str, JsonObject], run_dir: Path) -> None:
    outputs, input_prevouts = load_exported_block_script_metadata(run_dir)
    if not outputs and not input_prevouts:
        return

    for txid, tx_record in records.items():
        for input_record in tx_record.get("inputs", []):
            input_index = str(input_record.get("index"))
            prevout = input_prevouts.get((txid, input_index))
            if prevout is not None:
                apply_script_metadata(input_record, outputs.get(prevout))

        for output_record in tx_record.get("outputs", []):
            output_index = str(output_record.get("index"))
            apply_script_metadata(output_record, outputs.get((txid, output_index)))
