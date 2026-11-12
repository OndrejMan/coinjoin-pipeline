"""Typed shapes of the records the unified report compares.

``JsonObject`` stays the type of free-form JSON (scenario files, emulator
evidence, the rendered report).  The shapes here are the ones the report
*computes on*: both analyzers are normalized into :class:`TransactionRecord`
before they are compared.  Declaring them lets the type checker catch a renamed
field on one side of the comparison, which a plain ``dict`` never could.

The exporters also run inside the BlockSci image (Python 3.8), so every
subscripted type below appears only in annotations, which
``from __future__ import annotations`` leaves unevaluated.
"""

from __future__ import annotations

from typing import TypedDict


class _IORecordFields(TypedDict):
    index: str
    value: int | None
    address: str | None


class IORecord(_IORecordFields, total=False):
    """One normalized input or output, as either analyzer reports it."""

    wallet_name: str | None
    mix_event_type: str | None
    is_standard_denom: bool
    prev_txid: str | None
    spending_tx: str | None
    spend_by_tx: str | None


class TransactionMetrics(TypedDict):
    """Aggregates derived from a record's inputs and outputs."""

    input_count: int
    output_count: int
    total_input_sats: int
    total_output_sats: int
    repeated_output_denominations: dict[str, int]


class _TransactionFields(TypedDict):
    txid: str | None
    block_height: int | None
    inputs: list[IORecord]
    outputs: list[IORecord]


class TransactionRecord(_TransactionFields, TransactionMetrics, total=False):
    """One CoinJoin transaction normalized from coinjoin-analysis or BlockSci."""

    # coinjoin-analysis records when the round broadcast it, BlockSci its block's time.
    broadcast_time: str | None
    block_time: str | None

    round_id: str | None


class DetectorParameters(TypedDict):
    """Parameters a BlockSci analysis artifact was produced with."""

    coinjoin_type: str
    min_input_count: int | None


class BlockSciAnalysisArtifact(TypedDict):
    """``blocksci_analysis.json``: BlockSci output persisted for report assembly."""

    schema_version: str
    run_id: str
    parameters: DetectorParameters
    first_wasabi2_block: int
    records: dict[str, TransactionRecord]
