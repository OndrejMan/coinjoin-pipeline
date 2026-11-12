"""The run-directory layout: every relative path a run is made of, in one place.

The host package, the exporters and the tests import these names.
"""

from __future__ import annotations

import re
from pathlib import Path

BLOCKSCI_DIR = "blocksci_data"
BLOCKSCI_ANALYSIS_DIR = "blocksci-analysis_data"
REPORT_DIR = "coinjoinPipeline_data"
COINJOIN_ANALYSIS_DIR = "coinjoin-analysis_data"
EMULATOR_DIR = "coinjoin_emulator_data"
RUN_MANIFEST = "research_manifest.json"
SCENARIO_FILE = EMULATOR_DIR + "/scenario.json"
#: The wallet and coordinator logs coinjoin-analysis reads.
EMULATOR_DATA_DIR = EMULATOR_DIR + "/data"
BASELINE_FILE = COINJOIN_ANALYSIS_DIR + "/coinjoin_tx_info.json"
BLOCKSCI_CONFIG_FILE = BLOCKSCI_DIR + "/config.json"
ANALYSIS_ARTIFACT_NAME = "blocksci_analysis.json"
REPORT_JSON = "unified_report.json"
EXPORTED_BLOCKS_DIR = EMULATOR_DATA_DIR + "/btc-node"
#: One exported block below EXPORTED_BLOCKS_DIR: block_<height>.json.
EXPORTED_BLOCK_GLOB = "block_*.json"
_EXPORTED_BLOCK_NAME = re.compile(r"block_(\d+)\.json")
# A run the emulator has finished. The host writes RUN_MANIFEST before the
# emulator starts, so the manifest alone must not count here.
EMULATION_MARKERS = (SCENARIO_FILE,)


def block_height_from_path(path: Path) -> int | None:
    """The height an exported block_<height>.json file is named after; None for any other name."""
    match = _EXPORTED_BLOCK_NAME.fullmatch(path.name)
    return None if match is None else int(match.group(1))


def _tool_dir(run_dir: Path, name: str) -> Path:
    return run_dir / name


def blocksci_analysis_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, BLOCKSCI_ANALYSIS_DIR)


def report_dir(run_dir: Path) -> Path:
    return run_dir / REPORT_DIR
