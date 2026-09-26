"""Typed shapes of the records the unified report compares and the provenance it keeps.

``JsonObject`` stays the type of free-form JSON (scenario files, emulator
evidence, the rendered report).  The shapes here are the ones the report
*computes on*: both analyzers are normalized into :class:`TransactionRecord`
before they are compared, and :class:`RunManifest` is the provenance two runs
are diffed by.  Declaring them lets the type checker catch a renamed field on
one side of the comparison, which a plain ``dict`` never could.

The exporters also run inside the BlockSci image (Python 3.8), so every
subscripted type below appears only in annotations, which
``from __future__ import annotations`` leaves unevaluated.
"""

from __future__ import annotations

from typing import TypedDict

from exporters.common import JsonObject


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
    # Added from the exported blocks by script_metadata.apply_script_metadata.
    script_type: str
    script_asm: str
    script_hex: str
    address_type: str


class TransactionMetrics(TypedDict):
    """Aggregates derived from a record's inputs and outputs."""

    input_count: int
    output_count: int
    total_input_sats: int
    total_output_sats: int
    repeated_output_denominations: dict[str, int]


class _TransactionFields(TypedDict):
    txid: str | None
    broadcast_time: str | None
    block_height: int | None
    inputs: list[IORecord]
    outputs: list[IORecord]


class TransactionRecord(_TransactionFields, TransactionMetrics, total=False):
    """One CoinJoin transaction normalized from coinjoin-analysis or BlockSci."""

    round_id: str | None
    block_height_inferred: bool
    blocksci_heuristic_explanation: JsonObject


class RecordSummary(TypedDict):
    """The comparable part of a record quoted in the report's divergences."""

    block_height: int | None
    block_height_inferred: bool | None
    input_count: int | None
    output_count: int | None
    total_input_sats: int | None
    total_output_sats: int | None
    repeated_output_denominations: dict[str, int]
    wallets: list[str]


class _DetectorManifestFields(TypedDict):
    coinjoin_type: str
    blocksci_min_input_count: int | None


class DetectorManifest(_DetectorManifestFields, total=False):
    """Detector parameters; only the selected CoinJoin type's keys are present."""

    first_wasabi2_block: int
    joinmarket_detector: str
    joinmarket_min_base_fee: int
    joinmarket_percentage_fee: float
    joinmarket_max_depth: int


class ScenarioIdentity(TypedDict):
    name: str | None
    sha256: str | None


class ExecutionManifest(TypedDict):
    engine: str
    coinjoin_type: str
    reproduction_command: str | None


class _ImageFields(TypedDict):
    blocksci: str | None
    coinjoin_analysis: str | None
    coinjoin_emulator: str | None
    uploader: str | None
    unified_report: str | None


class ImageFields(_ImageFields, total=False):
    """Image references or digests; the mapping images exist only with mappings."""

    mappings_enumerator: str | None
    sake: str | None


class SourceCommits(TypedDict):
    coinjoin_emulator: str | None
    exporters: str | None


class SourceTrees(TypedDict):
    exporters_sha256: str | None
    exporters_git_dirty: bool | None


class _RunManifestFields(TypedDict):
    run_id: str
    scenario: ScenarioIdentity
    execution: ExecutionManifest
    detector: DetectorManifest
    images: ImageFields
    image_digests: ImageFields
    source_commits: SourceCommits
    source_trees: SourceTrees


class RunManifest(_RunManifestFields, total=False):
    """Provenance of one report; ``manifest.MANIFEST_COMPARE_FIELDS`` diffs it."""

    mode: str
    network: str | None
    mapping_parameters: JsonObject | None
    sake_seed: int | None


class DetectorParameters(TypedDict):
    """Parameters a BlockSci analysis artifact was produced with."""

    coinjoin_type: str
    min_input_count: int | None
    joinmarket_detector: str
    joinmarket_min_base_fee: int
    joinmarket_percentage_fee: float
    joinmarket_max_depth: int


class BlockSciAnalysisArtifact(TypedDict):
    """``blocksci_analysis.json``: BlockSci output persisted for report assembly."""

    schema_version: str
    run_id: str
    parameters: DetectorParameters
    first_wasabi2_block: int
    records: dict[str, TransactionRecord]
    skipped_txids: list[str]
    integration_diagnostics: JsonObject
    predicted_address_clusters: dict[str, str] | None
    cluster_export_error: str | None
