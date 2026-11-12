"""Script/address metadata extraction from exported regtest blocks."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Literal, Tuple

from exporters.common import JsonObject, to_json_text
from exporters.normalization import iter_exported_blocks
from exporters.report_types import IORecord, TransactionRecord


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


ScriptMetadataIndex = Tuple[Dict[Tuple[str, str], JsonObject], Dict[Tuple[str, str], Tuple[str, str]]]


def load_exported_block_script_metadata(run_dir: Path) -> ScriptMetadataIndex:
    """Script metadata of every exported output, and the outpoint each input spends."""
    outputs: dict[tuple[str, str], JsonObject] = {}
    input_prevouts: dict[tuple[str, str], tuple[str, str]] = {}
    for _path, _height, block in iter_exported_blocks(run_dir):
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


def apply_script_metadata(record: IORecord, metadata: JsonObject | None) -> None:
    if not metadata:
        return
    keys: tuple[Literal["script_type", "script_asm", "script_hex", "address_type"], ...] = (
        "script_type",
        "script_asm",
        "script_hex",
        "address_type",
    )
    for key in keys:
        if key in metadata and not record.get(key):
            record[key] = metadata[key]


def enrich_records_with_script_metadata(
    records: dict[str, TransactionRecord],
    run_dir: Path,
    index: ScriptMetadataIndex | None = None,
) -> None:
    """Copy script metadata onto each record's inputs and outputs.

    Pass ``index`` to enrich several record sets from one read of the blocks.
    """
    outputs, input_prevouts = index if index is not None else load_exported_block_script_metadata(run_dir)
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
