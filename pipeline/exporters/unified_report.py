#!/usr/bin/env python3
"""Public API facade for unified BlockSci-vs-emulator reports."""

# This module intentionally re-exports the legacy unified-report API while
# wrapping BlockSci-dependent entry points to keep their injected dependency in sync.
# pylint: disable=wildcard-import,unused-wildcard-import,function-redefined,unused-import

from __future__ import annotations

import builtins
import sys
from pathlib import Path

if not hasattr(builtins, "xrange"):
    setattr(builtins, "xrange", range)

try:
    import blocksci
except ImportError:
    pass

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from exporters.blocksci import detector as _blocksci_export
from exporters import cli as _cli
from exporters import integration_diagnostics as _integration_diagnostics
from exporters.blocksci.detector import *  # noqa: F403
from exporters.cli import *  # noqa: F403
from exporters.common import *  # noqa: F403
from exporters.common import (
    DEFAULT_CLUSTER_MAX_DISTANCE,
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    JsonObject,
)
from exporters.comparison import *  # noqa: F403
from exporters.emulator_data import *  # noqa: F403
from exporters.heuristics import *  # noqa: F403
from exporters.manifest import *  # noqa: F403
from exporters.normalization import *  # noqa: F403
from exporters.report_builder import *  # noqa: F403
from exporters.scenario import *  # noqa: F403
from exporters.script_metadata import *  # noqa: F403

blocksci = _blocksci_export.blocksci
build_chain_diagnostics = _integration_diagnostics.build_chain_diagnostics
build_image_diagnostics = _integration_diagnostics.build_image_diagnostics
build_integration_diagnostics = _integration_diagnostics.build_integration_diagnostics
build_target_diagnostics = _integration_diagnostics.build_target_diagnostics
call_joinmarket_detector = _integration_diagnostics.call_joinmarket_detector
docker_image_provenance = _integration_diagnostics.docker_image_provenance
exported_block_targets = _integration_diagnostics.exported_block_targets
normalize_joinmarket_detector_result = _integration_diagnostics.normalize_joinmarket_detector_result


def _sync_blocksci() -> None:
    _blocksci_export.blocksci = blocksci
    _cli.blocksci = blocksci


def main(argv: list[str] | None = None) -> int:  # type: ignore[no-redef]
    _sync_blocksci()
    return _cli.main(argv)


def export_blocksci_records(  # type: ignore[no-redef]
    config_path: Path,
    coinjoin_type: str,
    min_input_count: int | None,
    joinmarket_detector: str = DEFAULT_JOINMARKET_DETECTOR,
    joinmarket_min_base_fee: int = DEFAULT_JOINMARKET_MIN_BASE_FEE,
    joinmarket_percentage_fee: float = DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    joinmarket_max_depth: int = DEFAULT_JOINMARKET_MAX_DEPTH,
) -> tuple[dict[str, JsonObject], list[str]]:
    _sync_blocksci()
    return _blocksci_export.export_blocksci_records(
        config_path,
        coinjoin_type,
        min_input_count,
        joinmarket_detector,
        joinmarket_min_base_fee,
        joinmarket_percentage_fee,
        joinmarket_max_depth,
    )


def export_blocksci_cluster_assignments(  # type: ignore[no-redef]
    config_path: Path,
    emulator_data: JsonObject,
    coinjoin_type: str,
    output_dir: Path,
    max_distance: int = DEFAULT_CLUSTER_MAX_DISTANCE,
) -> tuple[dict[str, str] | None, str | None]:
    _sync_blocksci()
    return _blocksci_export.export_blocksci_cluster_assignments(
        config_path,
        emulator_data,
        coinjoin_type,
        output_dir,
        max_distance,
    )


if __name__ == "__main__":
    raise SystemExit(main())
