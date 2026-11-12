"""coinjoin-analysis and exported block normalization helpers."""

from __future__ import annotations

from pathlib import Path

from exporters.artifact_paths import (
    EXPORTED_BLOCK_GLOB,
    EXPORTED_BLOCKS_DIR,
    block_height_from_path,
)
from exporters.common import (
    load_json,
)


def exported_block_paths(run_dir: Path) -> list[Path]:
    """The emulator's exported ``block_<height>.json`` files, lowest height first."""
    block_dir = run_dir / EXPORTED_BLOCKS_DIR
    if not block_dir.is_dir():
        return []
    return sorted(block_dir.glob(EXPORTED_BLOCK_GLOB), key=lambda path: block_height_from_path(path) or -1)


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
