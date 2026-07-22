"""Locations for producer-owned artifacts within an emulation run."""

from __future__ import annotations

from pathlib import Path

BLOCKSCI_DIR = "blocksci_data"
BLOCKSCI_PARSE_DIR = "blocksci-parse_data"
BLOCKSCI_ANALYSIS_DIR = "blocksci-analysis_data"
BLOCKSCI_CUSTOM_ANALYSIS_DIR = "blocksci-custom-analysis_data"
BLOCKSCI_NOTEBOOKS_DIR = "blocksci-notebooks_data"
REPORT_DIR = "coinjoinPipeline_data"
COINJOIN_ANALYSIS_DIR = "coinjoin-analysis_data"
EMULATOR_DIR = "coinjoin_emulator_data"
MAPPINGS_DIR = "coinjoin-mappings_data"


def _tool_dir(run_dir: Path, name: str) -> Path:
    return run_dir / name


def emulator_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, EMULATOR_DIR)


def coinjoin_analysis_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, COINJOIN_ANALYSIS_DIR)


def blocksci_analysis_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, BLOCKSCI_ANALYSIS_DIR)


def blocksci_parse_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, BLOCKSCI_PARSE_DIR)


def report_dir(run_dir: Path) -> Path:
    return run_dir / REPORT_DIR


def mappings_dir(run_dir: Path) -> Path:
    return _tool_dir(run_dir, MAPPINGS_DIR)
