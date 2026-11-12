"""Run manifest construction and reproducibility comparisons."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from exporters.common import (
    JsonObject,
    digest_from_reference,
    docker_image_digest,
    first_present,
    nested_get,
    to_json_text,
    tree_sha256,
)
from exporters.parameters import IMAGE_COMPONENTS, Detector, ReportImages
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
    ("image_digests.blocksci", ("image_digests", "blocksci")),
    ("image_digests.coinjoin_analysis", ("image_digests", "coinjoin_analysis")),
    ("image_digests.coinjoin_emulator", ("image_digests", "coinjoin_emulator")),
    ("image_digests.uploader", ("image_digests", "uploader")),
    ("image_digests.unified_report", ("image_digests", "unified_report")),
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
    for component in IMAGE_COMPONENTS:
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
    # Every caller passes --engine (default $COINJOIN_ENGINE); a direct call without it guesses.
    inferred_engine = engine or ("joinmarket" if coinjoin_type == "joinmarket" else "wasabi")
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
        },
        # The report container sees the exporters without their checkout's .git (a
        # mount locally, a download from S3 on PBS), so no commit can be read there.
        # The tree hash names the exact code that produced the report in every case.
        # Informational only — nothing branches on it.
        "source_trees": {
            "exporters_sha256": tree_sha256(exporters_root),
        },
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
