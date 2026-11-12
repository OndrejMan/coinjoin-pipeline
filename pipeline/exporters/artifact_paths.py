"""The run-directory layout: every relative path a run is made of, in one place.

The host package, the exporters, the S3 PBS jobs and the tests import these names.
The jobs ship this module to the compute node, so it stays standard library only.
Shell job templates keep the paths spelled out for readability; a test checks that
they use only the directory names defined here.
"""

from __future__ import annotations

import re
from pathlib import Path

BLOCKSCI_DIR = "blocksci_data"
BLOCKSCI_ANALYSIS_DIR = "blocksci-analysis_data"
REPORT_DIR = "coinjoinPipeline_data"
COINJOIN_ANALYSIS_DIR = "coinjoin-analysis_data"
EMULATOR_DIR = "coinjoin_emulator_data"
#: S3 runs only: the emulator's Bitcoin datadir, which the BlockSci parse reads.
BITCOIN_DATA_DIR = "bitcoin_data"
#: S3 runs only: the exporters staged into the run prefix for the BlockSci job.
STAGED_EXPORTERS_DIR = ".pipeline/exporters"
#: PBS job logs of a run, uploaded next to its artifacts.
LOGS_DIR = "logs"

RUN_MANIFEST = "research_manifest.json"
SCENARIO_FILE = EMULATOR_DIR + "/scenario.json"
#: The wallet and coordinator logs coinjoin-analysis reads, and the emulator's labels.
EMULATOR_DATA_DIR = EMULATOR_DIR + "/data"
BASELINE_FILE = COINJOIN_ANALYSIS_DIR + "/coinjoin_tx_info.json"
# Every false-positive sidecar, including the numbered copies of extra inputs.
FALSE_CJTXS_GLOB = "false_cjtxs.json*"
# External baselines only: the Dumplings state the baseline was parsed from.
DUMPLINGS_SNAPSHOT_FILE = COINJOIN_ANALYSIS_DIR + "/dumplings-snapshot.json"
BLOCKSCI_CONFIG_FILE = BLOCKSCI_DIR + "/config.json"
ANALYSIS_ARTIFACT_NAME = "blocksci_analysis.json"
ANALYSIS_ARTIFACT_FILE = BLOCKSCI_ANALYSIS_DIR + "/" + ANALYSIS_ARTIFACT_NAME
REPORT_JSON = "unified_report.json"
REPORT_FILE = REPORT_DIR + "/" + REPORT_JSON
EXPORTED_BLOCKS_DIR = EMULATOR_DATA_DIR + "/btc-node"
#: One exported block below EXPORTED_BLOCKS_DIR: block_<height>.json.
EXPORTED_BLOCK_GLOB = "block_*.json"
_EXPORTED_BLOCK_NAME = re.compile(r"block_(\d+)\.json")
#: Directory of a run, and of its S3 prefix, holding job IDs and terminal markers:
#: <stage>.jobid|done|failed of the PBS stages.
PBS_MARKER_DIR = ".pbs"

# A run the emulator has finished. The host writes RUN_MANIFEST before the
# emulator starts, so the manifest alone must not count here.
EMULATION_MARKERS = (SCENARIO_FILE,)


def block_height_from_path(path: Path) -> int | None:
    """The height an exported block_<height>.json file is named after; None for any other name."""
    match = _EXPORTED_BLOCK_NAME.fullmatch(path.name)
    return None if match is None else int(match.group(1))


def pbs_marker(stage: str, suffix: str) -> str:
    """``.pbs/<stage>.<suffix>``, relative to the run directory or to the run's S3 prefix."""
    return f"{PBS_MARKER_DIR}/{stage}.{suffix}"


def _tool_dir(run_dir: Path, name: str) -> Path:
    return run_dir / name


def emulator_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, EMULATOR_DIR)


def coinjoin_analysis_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, COINJOIN_ANALYSIS_DIR)


def blocksci_analysis_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, BLOCKSCI_ANALYSIS_DIR)


def report_dir(run_dir: Path) -> Path:
    return run_dir / REPORT_DIR
