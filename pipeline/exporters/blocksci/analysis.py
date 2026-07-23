#!/usr/bin/env python3
"""Persist BlockSci detector, diagnostics, and clustering output for report assembly."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from exporters.artifact_paths import blocksci_analysis_dir, emulator_dir
from exporters.blocksci.detector import (
    blocksci,
    export_blocksci_cluster_assignments_for_addresses,
    export_blocksci_records,
)
from exporters.common import (
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    JsonObject,
    load_json,
    save_json,
)
from exporters.emulator_data import output_address
from exporters.integration_diagnostics import build_integration_diagnostics
from exporters.normalization import load_first_wasabi2_block

SCHEMA_VERSION = "1.0"
ARTIFACT_NAME = "blocksci_analysis.json"


def parse_min_input_count(value: str) -> int | None:
    normalized = value.strip().lower()
    if normalized in {"default", "none", "null"}:
        return None
    parsed = int(normalized)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer or 'default'")
    return parsed


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


def detector_parameters(args: argparse.Namespace) -> JsonObject:
    return {
        "coinjoin_type": args.coinjoin_type,
        "min_input_count": args.min_input_count,
        "test_values": args.test_values,
        "joinmarket_detector": args.joinmarket_detector,
        "joinmarket_min_base_fee": args.joinmarket_min_base_fee,
        "joinmarket_percentage_fee": args.joinmarket_percentage_fee,
        "joinmarket_max_depth": args.joinmarket_max_depth,
    }


def load_analysis(
    path: Path,
    *,
    run_id: str,
    expected_parameters: JsonObject,
) -> JsonObject:
    artifact = load_json(path)
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported BlockSci analysis schema in {path}: {artifact.get('schema_version')!r}"
        )
    if artifact.get("run_id") != run_id:
        raise ValueError(
            f"BlockSci analysis run mismatch: expected {run_id!r}, got {artifact.get('run_id')!r}"
        )
    if artifact.get("parameters") != expected_parameters:
        raise ValueError(
            "BlockSci analysis detector parameters do not match the requested report parameters"
        )
    if not isinstance(artifact.get("records"), dict):
        raise ValueError("BlockSci analysis artifact has invalid records")
    if not isinstance(artifact.get("skipped_txids"), list):
        raise ValueError("BlockSci analysis artifact has invalid skipped_txids")
    if not isinstance(artifact.get("first_wasabi2_block"), int):
        raise ValueError("BlockSci analysis artifact has invalid first_wasabi2_block")
    if not isinstance(artifact.get("integration_diagnostics"), dict):
        raise ValueError("BlockSci analysis artifact has invalid integration_diagnostics")
    clusters = artifact.get("predicted_address_clusters")
    if clusters is not None and not isinstance(clusters, dict):
        raise ValueError("BlockSci analysis artifact has invalid predicted_address_clusters")
    return artifact


def write_analysis(args: argparse.Namespace) -> Path:
    run_dir = args.run_dir.resolve()
    config_path = args.config.resolve()
    if blocksci is None:
        raise RuntimeError("BlockSci Python module is required to export BlockSci analysis.")

    blocksci.heuristics.set_test_values_enabled(args.test_values)
    records, skipped_txids = export_blocksci_records(
        config_path,
        args.coinjoin_type,
        args.min_input_count,
        joinmarket_detector=args.joinmarket_detector,
        joinmarket_min_base_fee=args.joinmarket_min_base_fee,
        joinmarket_percentage_fee=args.joinmarket_percentage_fee,
        joinmarket_max_depth=args.joinmarket_max_depth,
    )
    diagnostics = build_integration_diagnostics(
        run_dir,
        config_path,
        blocksci,
        records,
        args.coinjoin_type,
        {
            "blocksci": args.blocksci_image,
            "coinjoin_analysis": args.coinjoin_analysis_image,
            "coinjoin_emulator": args.coinjoin_emulator_image,
            "wrapper": args.wrapper_image,
        },
        joinmarket_detector=args.joinmarket_detector,
        joinmarket_min_base_fee=args.joinmarket_min_base_fee,
        joinmarket_percentage_fee=args.joinmarket_percentage_fee,
        joinmarket_max_depth=args.joinmarket_max_depth,
    )
    cluster_dir = config_path.parent / "clustering" / f"{args.coinjoin_type}_emulator_report"
    clusters, cluster_error = export_blocksci_cluster_assignments_for_addresses(
        config_path,
        exported_addresses(run_dir),
        args.coinjoin_type,
        cluster_dir,
    )
    artifact: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "parameters": detector_parameters(args),
        "first_wasabi2_block": load_first_wasabi2_block(config_path),
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
    parser.add_argument("--coinjoin-type", default="wasabi2")
    parser.add_argument("--min-input-count", type=parse_min_input_count, default=None)
    parser.add_argument("--test-values", action="store_true")
    parser.add_argument("--joinmarket-detector", default=DEFAULT_JOINMARKET_DETECTOR)
    parser.add_argument("--joinmarket-min-base-fee", type=int, default=DEFAULT_JOINMARKET_MIN_BASE_FEE)
    parser.add_argument(
        "--joinmarket-percentage-fee", type=float, default=DEFAULT_JOINMARKET_PERCENTAGE_FEE
    )
    parser.add_argument("--joinmarket-max-depth", type=int, default=DEFAULT_JOINMARKET_MAX_DEPTH)
    parser.add_argument("--blocksci-image", default=os.environ.get("BLOCKSCI_IMAGE"))
    parser.add_argument("--coinjoin-analysis-image", default=os.environ.get("COINJOIN_ANALYSIS_IMAGE"))
    parser.add_argument("--coinjoin-emulator-image", default=os.environ.get("COINJOIN_EMULATOR_IMAGE"))
    parser.add_argument("--wrapper-image", default=os.environ.get("WRAPPER_IMAGE"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    write_analysis(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
