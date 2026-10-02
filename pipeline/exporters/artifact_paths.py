"""The run-directory layout: every relative path a run is made of, in one place.

The host package, the exporters and the tests import these names. Shell job
templates keep the paths spelled out for readability; a test checks that they
use only the directory names defined here.
"""

from __future__ import annotations

from pathlib import Path

from exporters.common import file_sha256

BLOCKSCI_DIR = "blocksci_data"
BLOCKSCI_PARSE_DIR = "blocksci-parse_data"
BLOCKSCI_ANALYSIS_DIR = "blocksci-analysis_data"
BLOCKSCI_CUSTOM_ANALYSIS_DIR = "blocksci-custom-analysis_data"
BLOCKSCI_NOTEBOOKS_DIR = "blocksci-notebooks_data"
REPORT_DIR = "coinjoinPipeline_data"
COINJOIN_ANALYSIS_DIR = "coinjoin-analysis_data"
EMULATOR_DIR = "coinjoin_emulator_data"
MAPPINGS_DIR = "coinjoin-mappings_data"

RUN_MANIFEST = "research_manifest.json"
SCENARIO_FILE = EMULATOR_DIR + "/scenario.json"
LABEL_MANIFEST_FILE = EMULATOR_DIR + "/data/coinjoin_label_manifest.json"
ROUND_EVENTS_FILE = EMULATOR_DIR + "/data/joinmarket_round_events.json"
BASELINE_FILE = COINJOIN_ANALYSIS_DIR + "/coinjoin_tx_info.json"
FALSE_CJTXS_FILE = COINJOIN_ANALYSIS_DIR + "/false_cjtxs.json"
# Every false-positive sidecar, including the numbered copies of extra inputs.
FALSE_CJTXS_GLOB = "false_cjtxs.json*"
BLOCKSCI_CONFIG_FILE = BLOCKSCI_DIR + "/config.json"
ANALYSIS_ARTIFACT_NAME = "blocksci_analysis.json"
ANALYSIS_ARTIFACT_FILE = BLOCKSCI_ANALYSIS_DIR + "/" + ANALYSIS_ARTIFACT_NAME
MAPPINGS_FILE = MAPPINGS_DIR + "/coinjoin_mappings.json"
REPORT_JSON = "unified_report.json"
REPORT_MARKDOWN = "unified_report.md"
REPORT_FILE = REPORT_DIR + "/" + REPORT_JSON
REPORT_MARKDOWN_FILE = REPORT_DIR + "/" + REPORT_MARKDOWN
EXPORTED_BLOCKS_DIR = EMULATOR_DIR + "/data/btc-node"

# A run the emulator has finished. The host writes RUN_MANIFEST before the
# emulator starts, so the manifest alone must not count here.
EMULATION_MARKERS = (SCENARIO_FILE,)
# Anything `runs list` shows, including external runs that hold only a manifest.
CATALOG_MARKERS = (RUN_MANIFEST, EMULATOR_DIR, BLOCKSCI_DIR)
# Files the unified report is derived from, besides the false-positive
# sidecars. The report records their SHA-256, so a later change to any of them
# marks the report stale.
REPORT_INPUT_FILES = (
    SCENARIO_FILE,
    LABEL_MANIFEST_FILE,
    ROUND_EVENTS_FILE,
    BASELINE_FILE,
    ANALYSIS_ARTIFACT_FILE,
    MAPPINGS_FILE,
)


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


def mappings_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, MAPPINGS_DIR)


def report_input_files(run_dir: Path) -> list[str]:
    """Relative paths of the report inputs present in ``run_dir``."""
    present = [name for name in REPORT_INPUT_FILES if (run_dir / name).is_file()]
    sidecars = coinjoin_analysis_dir(run_dir).glob(FALSE_CJTXS_GLOB)
    present.extend(sorted(COINJOIN_ANALYSIS_DIR + "/" + path.name for path in sidecars if path.is_file()))
    return present


def report_input_hashes(run_dir: Path) -> dict[str, str]:
    """SHA-256 of each report input present in ``run_dir``, keyed by relative path."""
    return {name: file_sha256(run_dir / name) for name in report_input_files(run_dir)}
