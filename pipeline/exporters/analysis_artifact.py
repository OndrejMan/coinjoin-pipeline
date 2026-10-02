"""Versioned BlockSci analysis artifact, readable without importing BlockSci."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

from exporters.artifact_paths import ANALYSIS_ARTIFACT_NAME
from exporters.common import load_json
from exporters.report_types import BlockSciAnalysisArtifact, DetectorParameters

SCHEMA_VERSION = "1.1"
ARTIFACT_NAME = ANALYSIS_ARTIFACT_NAME


def detector_parameters(args: argparse.Namespace) -> DetectorParameters:
    return {
        "coinjoin_type": args.coinjoin_type,
        "min_input_count": args.min_input_count,
        "joinmarket_detector": args.joinmarket_detector,
        "joinmarket_min_base_fee": args.joinmarket_min_base_fee,
        "joinmarket_percentage_fee": args.joinmarket_percentage_fee,
        "joinmarket_max_depth": args.joinmarket_max_depth,
    }


def load_analysis(
    path: Path,
    *,
    run_id: str,
    expected_parameters: DetectorParameters,
    mode: str = "emulator",
) -> BlockSciAnalysisArtifact:
    """Load an artifact and check every field report assembly relies on."""
    artifact = load_json(path)
    if artifact.get("schema_version") not in {"1.0", SCHEMA_VERSION}:
        raise ValueError(f"Unsupported BlockSci analysis schema in {path}: {artifact.get('schema_version')!r}")
    if artifact.get("run_id") != run_id:
        raise ValueError(f"BlockSci analysis run mismatch: expected {run_id!r}, got {artifact.get('run_id')!r}")
    if artifact.get("parameters") != expected_parameters:
        raise ValueError("BlockSci analysis detector parameters do not match the requested report parameters")
    if not isinstance(artifact.get("records"), dict):
        raise ValueError("BlockSci analysis artifact has invalid records")
    if not isinstance(artifact.get("skipped_txids"), list):
        raise ValueError("BlockSci analysis artifact has invalid skipped_txids")
    if not isinstance(artifact.get("first_wasabi2_block"), int):
        raise ValueError("BlockSci analysis artifact has invalid first_wasabi2_block")
    if artifact.get("mode", "emulator") != mode:
        raise ValueError("BlockSci analysis mode does not match the requested report mode")
    if mode == "emulator" and not isinstance(artifact.get("integration_diagnostics"), dict):
        raise ValueError("BlockSci analysis artifact has invalid integration_diagnostics")
    clusters = artifact.get("predicted_address_clusters")
    if clusters is not None and not isinstance(clusters, dict):
        raise ValueError("BlockSci analysis artifact has invalid predicted_address_clusters")
    provenance = artifact.get("image_provenance")
    if provenance is not None:
        if not isinstance(provenance, dict):
            raise ValueError("BlockSci analysis artifact has invalid image_provenance")
        for recorded in provenance.values():
            if not isinstance(recorded, dict) or any(
                recorded.get(key) is not None and not isinstance(recorded[key], str)
                for key in ("reference", "image_id", "repo_digest")
            ):
                raise ValueError("BlockSci analysis artifact has invalid image_provenance entry")
    return cast(BlockSciAnalysisArtifact, artifact)
