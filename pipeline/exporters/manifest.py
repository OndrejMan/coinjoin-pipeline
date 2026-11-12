"""Run manifest construction."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from exporters.common import (
    JsonObject,
    digest_from_reference,
    docker_image_digest,
    first_present,
    git_commit_for_path,
    to_json_text,
)
from exporters.parameters import Detector, ReportImages
from exporters.report_types import ImageFields, RunManifest


def resolve_manifest_images(
    images: ImageFields,
    supplied_digests: ImageFields,
    provenance: JsonObject | None,
) -> tuple[ImageFields, ImageFields]:
    """Keep each recorded producer identity together, including unknown digests."""
    references = images.copy()
    digests = supplied_digests.copy()
    components: tuple[Literal["blocksci", "coinjoin_analysis", "coinjoin_emulator", "unified_report"], ...] = (
        "blocksci",
        "coinjoin_analysis",
        "coinjoin_emulator",
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
    }
