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

from exporters.analysis_artifact import ARTIFACT_NAME, SCHEMA_VERSION, detector_parameters
from exporters.artifact_paths import blocksci_analysis_dir, emulator_dir
from exporters.blocksci_export.detector import (
    BLOCKSCI_IMPORT_ERROR,
    blocksci,
    export_blocksci_cluster_assignments_for_addresses,
    export_blocksci_records,
)
from exporters.common import (
    digest_from_reference,
    load_json,
    save_json,
)
from exporters.emulator_data import output_address
from exporters.integration_diagnostics import build_integration_diagnostics
from exporters.normalization import load_first_wasabi2_block
from exporters.parameters import add_detector_arguments, add_image_arguments
from exporters.report_types import BlockSciAnalysisArtifact


def exported_addresses(run_dir: Path) -> set[str]:
    """Collect addresses from exported Bitcoin block JSON without baseline data."""
    addresses: set[str] = set()
    block_dir = emulator_dir(run_dir) / "data" / "btc-node"
    for block_path in sorted(block_dir.glob("block_*.json")):
        block = load_json(block_path)
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

    records, skipped_txids = export_blocksci_records(
        config_path,
        args.coinjoin_type,
        args.min_input_count,
        joinmarket_detector=args.joinmarket_detector,
        joinmarket_min_base_fee=args.joinmarket_min_base_fee,
        joinmarket_percentage_fee=args.joinmarket_percentage_fee,
        joinmarket_max_depth=args.joinmarket_max_depth,
    )
    diagnostics = (
        build_integration_diagnostics(
            run_dir,
            config_path,
            blocksci,
            records,
            args.coinjoin_type,
            {
                "blocksci": args.blocksci_image,
                "coinjoin_analysis": args.coinjoin_analysis_image,
                "coinjoin_emulator": args.coinjoin_emulator_image,
                "uploader": args.uploader_image,
                "unified_report": args.unified_report_image,
            },
            image_ids={
                name: getattr(args, name + "_image_id", None)
                for name in ("blocksci", "coinjoin_analysis", "coinjoin_emulator")
            },
            image_digests={
                **{
                    name: getattr(args, name + "_image_digest", None)
                    or digest_from_reference(getattr(args, name + "_image", None))
                    for name in ("blocksci", "coinjoin_analysis", "coinjoin_emulator")
                },
                "uploader": digest_from_reference(args.uploader_image),
                "unified_report": digest_from_reference(args.unified_report_image),
            },
            joinmarket_detector=args.joinmarket_detector,
            joinmarket_min_base_fee=args.joinmarket_min_base_fee,
            joinmarket_percentage_fee=args.joinmarket_percentage_fee,
            joinmarket_max_depth=args.joinmarket_max_depth,
        )
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
        "parameters": detector_parameters(args),
        "first_wasabi2_block": load_first_wasabi2_block(config_path),
        "image_provenance": {
            name: {
                "reference": getattr(args, name + "_image", None),
                "image_id": getattr(args, name + "_image_id", None),
                "repo_digest": getattr(args, name + "_image_digest", None)
                or digest_from_reference(getattr(args, name + "_image", None)),
                **((diagnostics or {}).get("images", {}).get(name) or {}),
            }
            for name in ("blocksci", "coinjoin_analysis", "coinjoin_emulator", "uploader")
        },
        "records": records,
        "skipped_txids": skipped_txids,
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
