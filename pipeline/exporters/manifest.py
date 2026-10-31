"""Run manifest construction and reproducibility comparisons."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from exporters.artifact_paths import report_input_hashes
from exporters.common import (
    JsonObject,
    digest_from_reference,
    docker_image_digest,
    first_present,
    git_commit_for_path,
    git_tree_is_dirty,
    nested_get,
    to_json_text,
    tree_sha256,
)
from exporters.parameters import Detector, ReportImages
from exporters.report_types import ImageFields, RunManifest

MANIFEST_COMPARE_FIELDS = (
    ("scenario.sha256", ("scenario", "sha256")),
    ("execution.engine", ("execution", "engine")),
    ("execution.coinjoin_type", ("execution", "coinjoin_type")),
    ("detector", ("detector",)),
    ("images.blocksci", ("images", "blocksci")),
    ("images.coinjoin_analysis", ("images", "coinjoin_analysis")),
    ("images.coinjoin_emulator", ("images", "coinjoin_emulator")),
    ("images.uploader", ("images", "uploader")),
    ("images.unified_report", ("images", "unified_report")),
    ("images.mappings_enumerator", ("images", "mappings_enumerator")),
    ("images.sake", ("images", "sake")),
    ("image_digests.blocksci", ("image_digests", "blocksci")),
    ("image_digests.coinjoin_analysis", ("image_digests", "coinjoin_analysis")),
    ("image_digests.coinjoin_emulator", ("image_digests", "coinjoin_emulator")),
    ("image_digests.uploader", ("image_digests", "uploader")),
    ("image_digests.unified_report", ("image_digests", "unified_report")),
    ("image_digests.mappings_enumerator", ("image_digests", "mappings_enumerator")),
    ("image_digests.sake", ("image_digests", "sake")),
    ("mapping_parameters", ("mapping_parameters",)),
    ("sake_seed", ("sake_seed",)),
    ("source_commits.coinjoin_emulator", ("source_commits", "coinjoin_emulator")),
)


def resolve_manifest_images(
    images: ImageFields,
    supplied_digests: ImageFields,
    provenance: JsonObject | None,
) -> tuple[ImageFields, ImageFields]:
    """Keep each recorded producer identity together, including unknown digests."""
    references = images.copy()
    digests = supplied_digests.copy()
    components: tuple[
        Literal["blocksci", "coinjoin_analysis", "coinjoin_emulator", "uploader", "unified_report"], ...
    ] = (
        "blocksci",
        "coinjoin_analysis",
        "coinjoin_emulator",
        "uploader",
        "unified_report",
    )
    for component in components:
        reference = images[component]
        recorded = (provenance or {}).get(component)
        if isinstance(recorded, dict) and (recorded.get("reference") or recorded.get("repo_digest")):
            # A tag may have moved since analysis. Neither the current daemon
            # nor this process's environment can fill a missing producer digest.
            reference = to_json_text(recorded.get("reference"))
            digest = to_json_text(recorded.get("repo_digest")) or digest_from_reference(reference)
        else:
            digest = (
                supplied_digests.get(component) or digest_from_reference(reference) or docker_image_digest(reference)
            )
        references[component] = reference
        digests[component] = digest
    return references, digests


def build_run_manifest(
    run_dir: Path,
    scenario: JsonObject | None,
    detector: Detector,
    engine: str | None = None,
    images: ReportImages | None = None,
    emulator_git_commit: str | None = None,
    image_provenance: JsonObject | None = None,
) -> RunManifest:
    exporters_root = Path(__file__).resolve().parent
    coinjoin_type = detector.coinjoin_type
    inferred_engine = engine or os.environ.get("COINJOIN_ENGINE")
    if not inferred_engine:
        inferred_engine = "joinmarket" if coinjoin_type == "joinmarket" else "wasabi"
    images = images or ReportImages()
    references, image_digests = resolve_manifest_images(
        images.reference_fields(), images.digest_fields(), image_provenance
    )
    return {
        "run_id": run_dir.name,
        "scenario": {
            "name": scenario.get("name") if scenario else None,
            "sha256": scenario.get("sha256") if scenario else None,
        },
        "execution": {
            "engine": inferred_engine,
            "coinjoin_type": coinjoin_type,
            "reproduction_command": os.environ.get("REPRODUCTION_COMMAND"),
        },
        "detector": detector.manifest(),
        "images": references,
        "image_digests": image_digests,
        "source_commits": {
            "coinjoin_emulator": first_present(
                emulator_git_commit,
                os.environ.get("COINJOIN_EMULATOR_GIT_COMMIT"),
            ),
            "exporters": git_commit_for_path(exporters_root),
        },
        # The report can run from a tree that has no checkout behind it: the PBS
        # node executes exporters downloaded from S3, where `git rev-parse`
        # yields null. The tree hash names the exact code that produced the
        # report in every case; `git_dirty` says whether the commit above is the
        # whole story. Informational only — nothing branches on them.
        "source_trees": {
            "exporters_sha256": tree_sha256(exporters_root),
            "exporters_git_dirty": git_tree_is_dirty(exporters_root),
        },
        "inputs": report_input_hashes(run_dir),
    }


def apply_mapping_provenance(run_manifest: RunManifest, coinjoin_mappings: JsonObject | None) -> None:
    """Record the mapping-stage images and parameters; only Wasabi runs with mappings have them."""
    if not coinjoin_mappings:
        return
    provenance = coinjoin_mappings.get("provenance") or {}
    run_manifest["images"]["mappings_enumerator"] = provenance.get("enumerator_image")
    run_manifest["images"]["sake"] = provenance.get("sake_image")
    run_manifest["image_digests"]["mappings_enumerator"] = provenance.get("enumerator_image_digest")
    run_manifest["image_digests"]["sake"] = provenance.get("sake_image_digest")
    run_manifest["mapping_parameters"] = (coinjoin_mappings.get("enumerator") or {}).get("parameters")
    run_manifest["sake_seed"] = (coinjoin_mappings.get("sake") or {}).get("seed")


def compare_run_manifests(
    previous_manifest: Mapping[str, object] | None,
    current_manifest: RunManifest,
) -> JsonObject:
    if not previous_manifest:
        return {
            "available": False,
            "reason": "No previous run manifest was available for comparison.",
            "matches": None,
            "differences": [],
        }

    differences = []
    for label, path in MANIFEST_COMPARE_FIELDS:
        previous_value = nested_get(previous_manifest, path)
        current_value = nested_get(current_manifest, path)
        if previous_value != current_value:
            differences.append(
                {
                    "field": label,
                    "previous": previous_value,
                    "current": current_value,
                }
            )

    return {
        "available": True,
        "matches": not differences,
        "differences": differences,
    }
