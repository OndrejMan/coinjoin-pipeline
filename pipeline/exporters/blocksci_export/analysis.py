#!/usr/bin/env python3
"""Persist BlockSci detector, diagnostics, and clustering output for report assembly."""

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
from exporters.blocksci_export.detector import (
    BLOCKSCI_IMPORT_ERROR,
    blocksci,
    export_blocksci_cluster_assignments_for_addresses,
    export_blocksci_records,
    export_subset_matching_records,
)
from exporters.common import save_json
from exporters.integration_diagnostics import build_integration_diagnostics
from exporters.normalization import iter_exported_blocks, load_first_wasabi2_block, output_address
from exporters.parameters import (
    IMAGE_COMPONENTS,
    Detector,
    ReportImages,
    add_detector_arguments,
    add_image_arguments,
)
from exporters.report_types import BlockSciAnalysisArtifact, SubsetMatchingAnalysis

# The report stage resolves its own image (manifest.resolve_manifest_images).
PROVENANCE_IMAGE_NAMES = IMAGE_COMPONENTS[:3]


def exported_addresses(run_dir: Path) -> set[str]:
    """Collect addresses from exported Bitcoin block JSON without baseline data."""
    addresses: set[str] = set()
    for _path, _height, block in iter_exported_blocks(run_dir):
        for tx in block.get("tx", []):
            for output in tx.get("vout", []):
                address = output_address(output)
                if address:
                    addresses.add(address)
    return addresses


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
    subset_matching: SubsetMatchingAnalysis | None = None
    if detector.coinjoin_type == "joinmarket" and getattr(args, "mode", "emulator") == "emulator":
        subset_records, skipped_txids = export_subset_matching_records(config_path, detector)
        subset_matching = SubsetMatchingAnalysis(records=subset_records, skipped_txids=skipped_txids)
    images = ReportImages.from_args(args)
    image_ids = {name: getattr(args, name + "_image_id", None) for name in PROVENANCE_IMAGE_NAMES}
    diagnostics = (
        build_integration_diagnostics(run_dir, config_path, blocksci, records, images, image_ids=image_ids)
        if getattr(args, "mode", "emulator") == "emulator"
        else None
    )
    cluster_dir = config_path.parent / "clustering" / f"{args.coinjoin_type}_emulator_report"
    clusters, cluster_error = (
        export_blocksci_cluster_assignments_for_addresses(
            config_path,
            exported_addresses(run_dir),
            records,
            getattr(args, "cluster_output_dir", None) or cluster_dir,
        )
        if not getattr(args, "skip_clustering", False)
        else (None, "Clustering was explicitly skipped during analysis.")
    )
    artifact: BlockSciAnalysisArtifact = {
        "schema_version": SCHEMA_VERSION,
        "mode": getattr(args, "mode", "emulator"),
        "run_id": run_dir.name,
        "parameters": detector.artifact_parameters(),
        "first_wasabi2_block": detector.first_wasabi2_block,
        "image_provenance": {
            name: {
                "reference": images.references[name],
                "image_id": getattr(args, name + "_image_id", None),
                "repo_digest": images.digests[name],
                **((diagnostics or {}).get("images", {}).get(name) or {}),
            }
            for name in PROVENANCE_IMAGE_NAMES
        },
        "records": records,
        "subset_matching": subset_matching,
        "integration_diagnostics": diagnostics,
        "predicted_address_clusters": clusters,
        "cluster_export_error": cluster_error,
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
    parser.add_argument("--mode", choices=("emulator", "external"), default="emulator")
    parser.add_argument("--skip-clustering", action="store_true")
    parser.add_argument("--cluster-output-dir", type=Path)
    add_detector_arguments(parser)
    add_image_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    write_analysis(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
