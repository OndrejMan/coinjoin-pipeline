"""Run manifest construction and reproducibility comparisons."""

from __future__ import annotations

import os
from pathlib import Path

from exporters.common import JsonObject, docker_image_digest, first_present, git_commit_for_path, nested_get

MANIFEST_COMPARE_FIELDS = (
    ("scenario.sha256", ("scenario", "sha256")),
    ("execution.engine", ("execution", "engine")),
    ("execution.coinjoin_type", ("execution", "coinjoin_type")),
    ("detector", ("detector",)),
    ("images.blocksci", ("images", "blocksci")),
    ("images.coinjoin_analysis", ("images", "coinjoin_analysis")),
    ("images.coinjoin_emulator", ("images", "coinjoin_emulator")),
    ("images.wrapper", ("images", "wrapper")),
    ("images.mappings_enumerator", ("images", "mappings_enumerator")),
    ("images.sake", ("images", "sake")),
    ("image_digests.blocksci", ("image_digests", "blocksci")),
    ("image_digests.coinjoin_analysis", ("image_digests", "coinjoin_analysis")),
    ("image_digests.coinjoin_emulator", ("image_digests", "coinjoin_emulator")),
    ("image_digests.wrapper", ("image_digests", "wrapper")),
    ("image_digests.mappings_enumerator", ("image_digests", "mappings_enumerator")),
    ("image_digests.sake", ("image_digests", "sake")),
    ("mapping_parameters", ("mapping_parameters",)),
    ("sake_seed", ("sake_seed",)),
    ("source_commits.coinjoin_emulator", ("source_commits", "coinjoin_emulator")),
)


def build_detector_manifest(
    coinjoin_type: str,
    min_input_count: int | None,
    test_values: bool,
    first_wasabi2_block: int,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
) -> JsonObject:
    detector: JsonObject = {
        "coinjoin_type": coinjoin_type,
        "blocksci_min_input_count": min_input_count,
        "blocksci_test_values": test_values,
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


def build_run_manifest(
    run_dir: Path,
    scenario: JsonObject | None,
    coinjoin_type: str,
    engine: str | None,
    min_input_count: int | None,
    test_values: bool,
    first_wasabi2_block: int,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
    blocksci_image: str | None = None,
    coinjoin_analysis_image: str | None = None,
    coinjoin_emulator_image: str | None = None,
    wrapper_image: str | None = None,
    blocksci_image_digest: str | None = None,
    coinjoin_analysis_image_digest: str | None = None,
    coinjoin_emulator_image_digest: str | None = None,
    wrapper_image_digest: str | None = None,
    emulator_git_commit: str | None = None,
) -> JsonObject:
    inferred_engine = engine or os.environ.get("COINJOIN_ENGINE")
    if not inferred_engine:
        inferred_engine = "joinmarket" if coinjoin_type == "joinmarket" else "wasabi"

    images = {
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
        "wrapper": first_present(wrapper_image, os.environ.get("WRAPPER_IMAGE")),
    }
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
            test_values,
            first_wasabi2_block,
            joinmarket_detector,
            joinmarket_min_base_fee,
            joinmarket_percentage_fee,
            joinmarket_max_depth,
        ),
        "images": images,
        "image_digests": {
            "blocksci": first_present(
                blocksci_image_digest,
                os.environ.get("BLOCKSCI_IMAGE_DIGEST"),
                docker_image_digest(images.get("blocksci")),
            ),
            "coinjoin_analysis": first_present(
                coinjoin_analysis_image_digest,
                os.environ.get("COINJOIN_ANALYSIS_IMAGE_DIGEST"),
                docker_image_digest(images.get("coinjoin_analysis")),
            ),
            "coinjoin_emulator": first_present(
                coinjoin_emulator_image_digest,
                os.environ.get("COINJOIN_EMULATOR_IMAGE_DIGEST"),
                os.environ.get("EMULATOR_IMAGE_DIGEST"),
                docker_image_digest(images.get("coinjoin_emulator")),
            ),
            "wrapper": first_present(
                wrapper_image_digest,
                os.environ.get("WRAPPER_IMAGE_DIGEST"),
                docker_image_digest(images.get("wrapper")),
            ),
        },
        "source_commits": {
            "coinjoin_emulator": first_present(
                emulator_git_commit,
                os.environ.get("COINJOIN_EMULATOR_GIT_COMMIT"),
            ),
            "exporters": git_commit_for_path(Path(__file__).resolve().parent),
        },
    }


def compare_run_manifests(
    previous_manifest: JsonObject | None,
    current_manifest: JsonObject,
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
