#!/usr/bin/env python3
"""Persist BlockSci detector output for report assembly."""

from __future__ import annotations

import argparse
import builtins
import sys
from pathlib import Path

if not hasattr(builtins, "xrange"):
    setattr(builtins, "xrange", range)

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from exporters.analysis_artifact import ARTIFACT_NAME, SCHEMA_VERSION
from exporters.artifact_paths import blocksci_analysis_dir
from exporters.blocksci_export.detector import BLOCKSCI_IMPORT_ERROR, blocksci, export_blocksci_records
from exporters.common import save_json
from exporters.normalization import load_first_wasabi2_block
from exporters.parameters import Detector, add_detector_arguments
from exporters.report_types import BlockSciAnalysisArtifact


def write_analysis(args: argparse.Namespace) -> Path:
    run_dir = args.run_dir.resolve()
    config_path = args.config.resolve()
    if blocksci is None:
        detail = f" Original import error: {BLOCKSCI_IMPORT_ERROR!r}." if BLOCKSCI_IMPORT_ERROR is not None else ""
        raise RuntimeError(
            f"BlockSci Python module is required to export BlockSci analysis.{detail}"
        ) from BLOCKSCI_IMPORT_ERROR

    detector = Detector.from_args(args, load_first_wasabi2_block(config_path))
    records = export_blocksci_records(config_path, detector)
    artifact: BlockSciAnalysisArtifact = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "parameters": detector.artifact_parameters(),
        "first_wasabi2_block": detector.first_wasabi2_block,
        "records": records,
    }
    output_dir = blocksci_analysis_dir(run_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / ARTIFACT_NAME
    save_json(output_path, artifact)
    print(f"BlockSci analysis saved to {output_path}")
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    add_detector_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    write_analysis(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
