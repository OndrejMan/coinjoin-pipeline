"""Versioned BlockSci analysis artifact, readable without importing BlockSci."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from exporters.artifact_paths import ANALYSIS_ARTIFACT_NAME
from exporters.common import load_json
from exporters.report_types import BlockSciAnalysisArtifact, DetectorParameters

SCHEMA_VERSION = "1.3"
ARTIFACT_NAME = ANALYSIS_ARTIFACT_NAME


def load_analysis(
    path: Path,
    *,
    run_id: str,
    expected_parameters: DetectorParameters,
) -> BlockSciAnalysisArtifact:
    """Load an artifact and check every field report assembly relies on."""
    artifact = load_json(path)
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported BlockSci analysis schema in {path}: {artifact.get('schema_version')!r}")
    if artifact.get("run_id") != run_id:
        raise ValueError(f"BlockSci analysis run mismatch: expected {run_id!r}, got {artifact.get('run_id')!r}")
    if artifact.get("parameters") != expected_parameters:
        raise ValueError("BlockSci analysis detector parameters do not match the requested report parameters")
    for field, kind in (("records", dict), ("first_wasabi2_block", int)):
        if not isinstance(artifact.get(field), kind):
            raise ValueError(f"BlockSci analysis artifact has invalid {field}")
    subset_matching = artifact.get("subset_matching")
    if subset_matching is not None and not (
        isinstance(subset_matching, dict)
        and isinstance(subset_matching.get("records"), dict)
        and isinstance(subset_matching.get("skipped_txids"), list)
    ):
        raise ValueError("BlockSci analysis artifact has invalid subset_matching")
    if not isinstance(artifact.get("integration_diagnostics"), dict):
        raise ValueError("BlockSci analysis artifact has invalid integration_diagnostics")
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
