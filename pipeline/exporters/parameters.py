"""Detector and image options shared by analysis and report input boundaries."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field

from exporters.common import DEFAULT_FIRST_WASABI2_BLOCK, digest_from_reference
from exporters.report_types import DetectorManifest, DetectorParameters, ImageFields

# Images whose reference and digest a report records.
IMAGE_COMPONENTS = ("blocksci", "coinjoin_analysis", "coinjoin_emulator", "unified_report")


@dataclass(frozen=True)
class Detector:
    """The BlockSci detector one analysis ran with and its report explains."""

    coinjoin_type: str = "wasabi2"
    # None selects BlockSci's height-aware production threshold.
    min_input_count: int | None = None
    # Read from the parsed index, not an option.
    first_wasabi2_block: int = DEFAULT_FIRST_WASABI2_BLOCK

    @classmethod
    def from_args(cls, args: argparse.Namespace, first_wasabi2_block: int = DEFAULT_FIRST_WASABI2_BLOCK) -> Detector:
        return cls(
            coinjoin_type=args.coinjoin_type,
            min_input_count=args.min_input_count,
            first_wasabi2_block=first_wasabi2_block,
        )

    def artifact_parameters(self) -> DetectorParameters:
        """The options an analysis artifact records; a report must request the same ones."""
        return {
            "coinjoin_type": self.coinjoin_type,
            "min_input_count": self.min_input_count,
        }

    def manifest(self) -> DetectorManifest:
        """Only the selected CoinJoin type's parameters."""
        detector: DetectorManifest = {
            "coinjoin_type": self.coinjoin_type,
            "blocksci_min_input_count": self.min_input_count,
        }
        if self.coinjoin_type == "wasabi2":
            detector["first_wasabi2_block"] = self.first_wasabi2_block
        return detector


@dataclass(frozen=True)
class ReportImages:
    """Image references by component, and the digest each one was pinned or inspected at."""

    references: dict[str, str | None] = field(default_factory=dict)
    digests: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> ReportImages:
        # The report image has no digest option; a pinned reference carries its own.
        references = {name: getattr(args, name + "_image", None) for name in IMAGE_COMPONENTS}
        return cls(
            references,
            {
                name: getattr(args, name + "_image_digest", None) or digest_from_reference(references[name])
                for name in IMAGE_COMPONENTS
            },
        )

    def reference_fields(self) -> ImageFields:
        return _image_fields(self.references)

    def digest_fields(self) -> ImageFields:
        return _image_fields(self.digests)


def _image_fields(values: dict[str, str | None]) -> ImageFields:
    return {
        "blocksci": values.get("blocksci"),
        "coinjoin_analysis": values.get("coinjoin_analysis"),
        "coinjoin_emulator": values.get("coinjoin_emulator"),
        "unified_report": values.get("unified_report"),
    }


def parse_min_input_count(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"", "none", "null", "default"}:
        return None
    try:
        parsed = int(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer or 'default'") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def add_detector_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--coinjoin-type", default=os.environ.get("BLOCKSCI_COINJOIN_TYPE", "wasabi2"))
    parser.add_argument(
        "--min-input-count",
        type=parse_min_input_count,
        default=os.environ.get("BLOCKSCI_MIN_INPUT_COUNT"),
        help="Override BlockSci detector min input count; use 'default' for BlockSci's default.",
    )


def add_image_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--blocksci-image", default=os.environ.get("BLOCKSCI_IMAGE"))
    parser.add_argument("--coinjoin-analysis-image", default=os.environ.get("COINJOIN_ANALYSIS_IMAGE"))
    parser.add_argument(
        "--coinjoin-emulator-image",
        default=os.environ.get("COINJOIN_EMULATOR_IMAGE") or os.environ.get("EMULATOR_IMAGE"),
    )
    parser.add_argument("--unified-report-image", default=os.environ.get("COINJOIN_UNIFIED_REPORT_IMAGE"))
    parser.add_argument("--blocksci-image-digest", default=os.environ.get("BLOCKSCI_IMAGE_DIGEST"))
    parser.add_argument("--blocksci-image-id", default=os.environ.get("BLOCKSCI_IMAGE_ID"))
    parser.add_argument(
        "--coinjoin-analysis-image-digest",
        default=os.environ.get("COINJOIN_ANALYSIS_IMAGE_DIGEST"),
    )
    parser.add_argument("--coinjoin-analysis-image-id", default=os.environ.get("COINJOIN_ANALYSIS_IMAGE_ID"))
    parser.add_argument(
        "--coinjoin-emulator-image-digest",
        default=os.environ.get("COINJOIN_EMULATOR_IMAGE_DIGEST") or os.environ.get("EMULATOR_IMAGE_DIGEST"),
    )
    parser.add_argument(
        "--coinjoin-emulator-image-id",
        default=os.environ.get("COINJOIN_EMULATOR_IMAGE_ID") or os.environ.get("EMULATOR_IMAGE_ID"),
    )
