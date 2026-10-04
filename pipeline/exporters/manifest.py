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
from exporters.report_types import DetectorManifest, ImageFields, RunManifest

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


def build_detector_manifest(
    coinjoin_type: str,
    min_input_count: int | None,
    first_wasabi2_block: int,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
) -> DetectorManifest:
    detector: DetectorManifest = {
        "coinjoin_type": coinjoin_type,
        "blocksci_min_input_count": min_input_count,
    }
    if coinjoin_type == "wasabi2":
        detector["first_wasabi2_block"] = first_wasabi2_block
    if coinjoin_type == "joinmarket":
        detector.update(
            {
                "joinmarket_detector": joinmarket_detector,
                "joinmarket_min_base_fee": joinmarket_min_base_fee,
                "joinmarket_percentage_fee": joinmarket_percentage_fee,
                "joinmarket_max_depth": joinmarket_max_depth,
            }
        )
    return detector


def resolve_manifest_images(
    images: ImageFields,
    supplied_digests: ImageFields,
    provenance: JsonObject | None,
) -> tuple[ImageFields, ImageFields]:
    """Keep each recorded producer identity together, including unknown digests."""
    references = images.copy()
    digests = supplied_digests.copy()
    components: tuple[Literal["blocksci", "coinjoin_analysis", "coinjoin_emulator", "uploader", "unified_report"], ...] = (
        "blocksci", "coinjoin_analysis", "coinjoin_emulator", "uploader", "unified_report",
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
            digest = supplied_digests.get(component) or digest_from_reference(reference) or docker_image_digest(reference)
        references[component] = reference
        digests[component] = digest
    return references, digests


def build_run_manifest(
    run_dir: Path,
    scenario: JsonObject | None,
    coinjoin_type: str,
    engine: str | None,
    min_input_count: int | None,
    first_wasabi2_block: int,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
    blocksci_image: str | None = None,
    coinjoin_analysis_image: str | None = None,
    coinjoin_emulator_image: str | None = None,
    uploader_image: str | None = None,
    unified_report_image: str | None = None,
    blocksci_image_digest: str | None = None,
    coinjoin_analysis_image_digest: str | None = None,
    coinjoin_emulator_image_digest: str | None = None,
    uploader_image_digest: str | None = None,
    unified_report_image_digest: str | None = None,
    emulator_git_commit: str | None = None,
    image_provenance: JsonObject | None = None,
) -> RunManifest:
    exporters_root = Path(__file__).resolve().parent
    inferred_engine = engine or os.environ.get("COINJOIN_ENGINE")
    if not inferred_engine:
        inferred_engine = "joinmarket" if coinjoin_type == "joinmarket" else "wasabi"

    images: ImageFields = {
        "blocksci": first_present(blocksci_image, os.environ.get("BLOCKSCI_IMAGE")),
        "coinjoin_analysis": first_present(
            coinjoin_analysis_image,
            os.environ.get("COINJOIN_ANALYSIS_IMAGE"),
        ),
        "coinjoin_emulator": first_present(
            coinjoin_emulator_image,
            os.environ.get("COINJOIN_EMULATOR_IMAGE"),
            os.environ.get("EMULATOR_IMAGE"),
        ),
        "uploader": first_present(uploader_image, os.environ.get("COINJOIN_UPLOADER_IMAGE")),
        "unified_report": first_present(unified_report_image, os.environ.get("COINJOIN_UNIFIED_REPORT_IMAGE")),
    }
    images, image_digests = resolve_manifest_images(
        images,
        {
            "blocksci": first_present(blocksci_image_digest, os.environ.get("BLOCKSCI_IMAGE_DIGEST")),
            "coinjoin_analysis": first_present(
                coinjoin_analysis_image_digest, os.environ.get("COINJOIN_ANALYSIS_IMAGE_DIGEST"),
            ),
            "coinjoin_emulator": first_present(
                coinjoin_emulator_image_digest,
                os.environ.get("COINJOIN_EMULATOR_IMAGE_DIGEST"),
                os.environ.get("EMULATOR_IMAGE_DIGEST"),
            ),
            "uploader": uploader_image_digest,
            "unified_report": unified_report_image_digest,
        },
        image_provenance,
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
        "detector": build_detector_manifest(
            coinjoin_type,
            min_input_count,
            first_wasabi2_block,
            joinmarket_detector,
            joinmarket_min_base_fee,
            joinmarket_percentage_fee,
            joinmarket_max_depth,
        ),
        "images": images,
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
