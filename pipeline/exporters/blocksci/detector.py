"""BlockSci detector and clustering adapters."""

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


def _iter_attr(obj: object, name: str) -> Iterable[object]:
    """Return the named attribute if it is iterable, else an empty iterable."""
    value = safe_attr(obj, name, [])
    return value if isinstance(value, Iterable) else []


def _base_io_record(item: object, fallback_index: int) -> JsonObject:
    """Build the fields shared by every input and output record."""
    return {
        "index": str(safe_attr(item, "index", fallback_index)),
        "value": coerce_sats(safe_attr(item, "value")),
        "address": to_json_text(safe_attr(item, "address")),
    }


def _referenced_txid(item: object, tx_attr: str) -> str | None:
    """Resolve the hash of a tx referenced by ``item`` via ``tx_attr``."""
    referenced_tx = safe_attr(item, tx_attr)
    if referenced_tx is None:
        return None
    return to_json_text(safe_attr(referenced_tx, "hash"))


def _normalize_input(input_value: object, fallback_index: int) -> JsonObject:
    record = _base_io_record(input_value, fallback_index)
    spent_txid = _referenced_txid(input_value, "spent_tx")
    spent_index = safe_attr(input_value, "spent_tx_index")
    if spent_txid is not None and spent_index is not None:
        record["spending_tx"] = f"vout_{spent_txid}_{spent_index}"
    return record


def _normalize_output(output_value: object, fallback_index: int) -> JsonObject:
    record = _base_io_record(output_value, fallback_index)
    spending_txid = _referenced_txid(output_value, "spending_tx")
    spending_index = safe_attr(output_value, "spending_tx_index")
    if spending_txid is not None and spending_index is not None:
        record["spend_by_tx"] = f"vin_{spending_txid}_{spending_index}"
    return record


def _sorted_by_index(records: list[JsonObject]) -> list[JsonObject]:
    return sorted(records, key=lambda item: int(str(item["index"])))


def normalize_blocksci_tx(tx: object) -> JsonObject:
    inputs = [
        _normalize_input(input_value, index)
        for index, input_value in enumerate(_iter_attr(tx, "inputs"))
    ]
    outputs = [
        _normalize_output(output_value, index)
        for index, output_value in enumerate(_iter_attr(tx, "outputs"))
    ]

    record = {
        "txid": to_json_text(safe_attr(tx, "hash")),
        "broadcast_time": to_json_text(safe_attr(tx, "block_time")),
        "block_height": safe_attr(tx, "block_height"),
        "inputs": _sorted_by_index(inputs),
        "outputs": _sorted_by_index(outputs),
    }
    return add_common_metrics(record)


def _require_binding(chain: object, method: str, hint: str) -> None:
    """Fail loudly when the BlockSci build lacks the required report binding."""
    if not hasattr(chain, method):
        raise RuntimeError(
            f"This BlockSci build does not expose Blockchain.{method}; rebuild BlockSci with the {hint}."
        )


def _filter_joinmarket_txes(
    chain: object,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
) -> tuple[Iterable[object], list[str]]:
    _require_binding(chain, "filter_joinmarket_txes", "JoinMarket report binding")
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
    return txes, skipped_txids


def _filter_raw_coinjoin_txes(
    chain: object,
    coinjoin_type: str,
    min_input_count: int | None,
) -> Iterable[object]:
    _require_binding(chain, "filter_coinjoin_txes_raw", "raw CoinJoin report binding")
    if min_input_count is None:
        return chain.filter_coinjoin_txes_raw(0, len(chain), coinjoin_type)
    return chain.filter_coinjoin_txes_raw(0, len(chain), coinjoin_type, min_input_count)


def _records_by_txid(txes: Iterable[object]) -> dict[str, JsonObject]:
    records = [normalize_blocksci_tx(tx) for tx in txes]
    return {
        record["txid"]: record
        for record in sorted(records, key=lambda item: item["txid"] or "")
        if record["txid"]
    }


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

    if coinjoin_type == "joinmarket":
        txes, skipped_txids = _filter_joinmarket_txes(
            chain,
            joinmarket_detector,
            joinmarket_min_base_fee,
            joinmarket_percentage_fee,
            joinmarket_max_depth,
        )
    else:
        txes = _filter_raw_coinjoin_txes(chain, coinjoin_type, min_input_count)
        skipped_txids = []

    return _records_by_txid(txes), skipped_txids


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

    return export_blocksci_cluster_assignments_for_addresses(
        config_path,
        labels_by_address,
        coinjoin_type,
        output_dir,
        max_distance,
    )


def _run_coinjoin_clustering(
    chain: object,
    output_dir: Path,
    coinjoin_type: str,
    max_distance: int,
) -> object:
    """Build the CoinJoin cluster manager for the whole chain."""
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    return blocksci.cluster.CoinjoinClusterManager.create_clustering(
        chain=chain,
        start=0,
        stop=-1,
        heuristic_func=build_default_coinjoin_clustering_heuristic(),
        output_path=str(output_dir),
        overwrite=True,
        coinjoin_type=coinjoin_type,
        max_distance=max_distance,
    )


def _cluster_index_for_address(chain: object, clusterer: object, address_text: str) -> str | None:
    """Return the cluster index for ``address_text``, or None if it is not comparable."""
    try:
        address = chain.address_from_string(address_text)
        if not address:
            return None
        cluster = clusterer.cluster_with_address(address)
        address_count = safe_attr(cluster, "address_count")
        if callable(address_count) and address_count() == 0:
            return None
        cluster_index = safe_attr(cluster, "index")
        if cluster_index is None:
            return None
        return str(cluster_index)
    except Exception:
        return None


def export_blocksci_cluster_assignments_for_addresses(
    config_path: Path,
    addresses: Iterable[str],
    coinjoin_type: str,
    output_dir: Path,
    max_distance: int = DEFAULT_CLUSTER_MAX_DISTANCE,
) -> tuple[dict[str, str] | None, str | None]:
    """Cluster the chain and return assignments for the requested addresses."""
    requested_addresses = sorted({str(address) for address in addresses if address})
    if not requested_addresses:
        return None, "No addresses were supplied for BlockSci clustering."

    try:
        chain = blocksci.Blockchain(str(config_path))
        clusterer = _run_coinjoin_clustering(chain, output_dir, coinjoin_type, max_distance)
    except Exception as exc:
        return None, f"BlockSci cluster assignment export failed: {exc}"

    predicted: dict[str, str] = {}
    skipped = 0
    for address_text in requested_addresses:
        cluster_index = _cluster_index_for_address(chain, clusterer, address_text)
        if cluster_index is None:
            skipped += 1
            continue
        predicted[address_text] = cluster_index

    if not predicted:
        return None, f"BlockSci clustering produced no comparable address labels; skipped {skipped} addresses."
    return predicted, None
