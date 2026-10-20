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
from exporters.artifact_paths import BASELINE_FILE, blocksci_analysis_dir, emulator_dir
from exporters.blocksci_export.detector import (
    BLOCKSCI_IMPORT_ERROR,
    baseline_block_heights,
    blocksci,
    export_blocksci_cluster_assignments_for_addresses,
    export_blocksci_mix_events,
    export_blocksci_records,
    open_chain,
    scan_window,
)
from exporters.common import (
    digest_from_reference,
    load_json,
    save_json,
)
from exporters.emulator_data import output_address
from exporters.integration_diagnostics import build_integration_diagnostics
from exporters.normalization import load_first_wasabi2_block
from exporters.parameters import add_detector_arguments, add_image_arguments, add_window_arguments
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

    pin_height = getattr(args, "pin_height", None)
    max_block = None if pin_height is None else pin_height + 1
    start_height = getattr(args, "scan_start_height", None)
    end_height = getattr(args, "scan_end_height", None)
    records, skipped_txids = export_blocksci_records(
        config_path,
        args.coinjoin_type,
        args.min_input_count,
        joinmarket_detector=args.joinmarket_detector,
        joinmarket_min_base_fee=args.joinmarket_min_base_fee,
        joinmarket_percentage_fee=args.joinmarket_percentage_fee,
        joinmarket_max_depth=args.joinmarket_max_depth,
        max_block=max_block,
        start_height=start_height,
        end_height=end_height,
    )
    window_record, baseline_heights = None, None
    if getattr(args, "mode", "emulator") == "external":
        # The comparison may only judge heights both analyzers saw.
        chain = open_chain(config_path, max_block)
        start, end = scan_window(chain, start_height, end_height)
        window_record = {
            "start_height": start,
            "end_height": end,
            "end_exclusive": True,
            "index_blocks": len(chain),
            "index_last_block_hash": str(chain[len(chain) - 1].hash),
            "pin_height": pin_height,
        }
        baseline_path = run_dir / BASELINE_FILE
        if baseline_path.is_file():
            baseline_heights = baseline_block_heights(chain, load_json(baseline_path).get("coinjoins", {}))
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
    mix_events, mix_events_error = (
        export_blocksci_mix_events(config_path, records)
        if getattr(args, "mode", "emulator") == "emulator"
        else (None, "Liquidity events are classified only for emulator runs.")
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
        "mix_events": mix_events,
        "mix_events_error": mix_events_error,
        "scan_window": window_record,
        "baseline_heights": baseline_heights,
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
    add_window_arguments(parser)
    add_image_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    write_analysis(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
