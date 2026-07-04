"""BlockSci export and clustering adapters."""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from pathlib import Path

from exporters.common import (
    DEFAULT_CLUSTER_MAX_DISTANCE,
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    JsonObject,
    add_common_metrics,
    coerce_sats,
    safe_attr,
    to_json_text,
)
from exporters.emulator_data import wallet_address_labels


def prepare_blocksci_import() -> None:
    """Keep legacy pycrypto imports working in Python 3 BlockSci images."""
    if not hasattr(builtins, "xrange"):
        setattr(builtins, "xrange", range)


try:
    prepare_blocksci_import()
    import blocksci
except ImportError:  # pragma: no cover - exercised in environments without BlockSci.
    blocksci = None


def normalize_blocksci_tx(tx: object) -> JsonObject:
    txid = to_json_text(safe_attr(tx, "hash"))
    block_time = safe_attr(tx, "block_time")
    block_height = safe_attr(tx, "block_height")
    inputs: list[JsonObject] = []
    outputs: list[JsonObject] = []

    raw_inputs = safe_attr(tx, "inputs", [])
    inputs_iterable = raw_inputs if isinstance(raw_inputs, Iterable) else []
    for input_value in inputs_iterable:
        spent_tx = safe_attr(input_value, "spent_tx")
        spent_txid = to_json_text(safe_attr(spent_tx, "hash")) if spent_tx is not None else None
        spent_index = safe_attr(input_value, "spent_tx_index")
        input_record = {
            "index": str(safe_attr(input_value, "index", len(inputs))),
            "value": coerce_sats(safe_attr(input_value, "value")),
            "address": to_json_text(safe_attr(input_value, "address")),
        }
        if spent_txid is not None and spent_index is not None:
            input_record["spending_tx"] = f"vout_{spent_txid}_{spent_index}"
        inputs.append(input_record)

    raw_outputs = safe_attr(tx, "outputs", [])
    outputs_iterable = raw_outputs if isinstance(raw_outputs, Iterable) else []
    for output_value in outputs_iterable:
        spending_tx = safe_attr(output_value, "spending_tx")
        spending_txid = to_json_text(safe_attr(spending_tx, "hash")) if spending_tx is not None else None
        spending_index = safe_attr(output_value, "spending_tx_index")
        output_record = {
            "index": str(safe_attr(output_value, "index", len(outputs))),
            "value": coerce_sats(safe_attr(output_value, "value")),
            "address": to_json_text(safe_attr(output_value, "address")),
        }
        if spending_txid is not None and spending_index is not None:
            output_record["spend_by_tx"] = f"vin_{spending_txid}_{spending_index}"
        outputs.append(output_record)

    record = {
        "txid": txid,
        "broadcast_time": to_json_text(block_time),
        "block_height": block_height,
        "inputs": sorted(inputs, key=lambda item: int(str(item["index"]))),
        "outputs": sorted(outputs, key=lambda item: int(str(item["index"]))),
    }
    return add_common_metrics(record)


def export_blocksci_records(
    config_path: Path,
    coinjoin_type: str,
    min_input_count: int | None,
    joinmarket_detector: str = DEFAULT_JOINMARKET_DETECTOR,
    joinmarket_min_base_fee: int = DEFAULT_JOINMARKET_MIN_BASE_FEE,
    joinmarket_percentage_fee: float = DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    joinmarket_max_depth: int = DEFAULT_JOINMARKET_MAX_DEPTH,
) -> tuple[dict[str, JsonObject], list[str]]:
    if blocksci is None:
        raise RuntimeError("BlockSci Python module is required to export BlockSci records.")
    chain = blocksci.Blockchain(str(config_path))
    skipped_txids: list[str] = []
    if coinjoin_type == "joinmarket":
        if not hasattr(chain, "filter_joinmarket_txes"):
            raise RuntimeError(
                "This BlockSci build does not expose Blockchain.filter_joinmarket_txes; "
                "rebuild BlockSci with the JoinMarket report binding."
            )
        txes, skipped = chain.filter_joinmarket_txes(
            0,
            len(chain),
            joinmarket_detector,
            joinmarket_min_base_fee,
            joinmarket_percentage_fee,
            joinmarket_max_depth,
        )
        skipped_txids = sorted(
            txid
            for txid in (to_json_text(safe_attr(tx, "hash")) for tx in skipped)
            if txid
        )
    elif min_input_count is None:
        txes = chain.filter_coinjoin_txes(0, len(chain), coinjoin_type)
    else:
        txes = chain.filter_coinjoin_txes(0, len(chain), coinjoin_type, min_input_count)

    records = [normalize_blocksci_tx(tx) for tx in txes]
    return {
        record["txid"]: record
        for record in sorted(records, key=lambda item: item["txid"] or "")
        if record["txid"]
    }, skipped_txids


def build_default_coinjoin_clustering_heuristic() -> object:
    coinjoin_heuristics = blocksci.heuristics.coinjoin
    return (
        coinjoin_heuristics.one_output_consolidation_2hops
        & coinjoin_heuristics.two_equal_output_consolidation_1hop
    )


def export_blocksci_cluster_assignments(
    config_path: Path,
    emulator_data: JsonObject | None,
    coinjoin_type: str,
    output_dir: Path,
    max_distance: int = DEFAULT_CLUSTER_MAX_DISTANCE,
) -> tuple[dict[str, str] | None, str | None]:
    if blocksci is None:
        return None, "BlockSci Python module is required for clustering evaluation."
    labels_by_address = wallet_address_labels(emulator_data)
    if not labels_by_address:
        return None, "No emulator wallet address labels are available for clustering evaluation."

    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        chain = blocksci.Blockchain(str(config_path))
        heuristic = build_default_coinjoin_clustering_heuristic()
        clusterer = blocksci.cluster.CoinjoinClusterManager.create_clustering(
            chain=chain,
            start=0,
            stop=-1,
            heuristic_func=heuristic,
            output_path=str(output_dir),
            overwrite=True,
            coinjoin_type=coinjoin_type,
            max_distance=max_distance,
        )
    except Exception as exc:
        return None, f"BlockSci cluster assignment export failed: {exc}"

    predicted: dict[str, str] = {}
    skipped = 0
    for address_text in sorted(labels_by_address):
        try:
            address = chain.address_from_string(address_text)
            if not address:
                skipped += 1
                continue
            cluster = clusterer.cluster_with_address(address)
            address_count = safe_attr(cluster, "address_count")
            if callable(address_count) and address_count() == 0:
                skipped += 1
                continue
            cluster_index = safe_attr(cluster, "index")
            if cluster_index is None:
                skipped += 1
                continue
            predicted[address_text] = str(cluster_index)
        except Exception:
            skipped += 1

    if not predicted:
        return None, f"BlockSci clustering produced no comparable address labels; skipped {skipped} addresses."
    return predicted, None
