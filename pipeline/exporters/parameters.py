"""Detector and image options shared by analysis and report input boundaries."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field

from exporters.common import (
    DEFAULT_FIRST_WASABI2_BLOCK,
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    digest_from_reference,
)
from exporters.report_types import DetectorManifest, DetectorParameters, ImageFields

# Images whose reference and digest a report records.
IMAGE_COMPONENTS = ("blocksci", "coinjoin_analysis", "coinjoin_emulator", "uploader", "unified_report")


@dataclass(frozen=True)
class Detector:
    """The BlockSci detector one analysis ran with and its report explains."""

    coinjoin_type: str = "wasabi2"
    # None selects BlockSci's height-aware production threshold.
    min_input_count: int | None = None
    # Read from the parsed index, not an option.
    first_wasabi2_block: int = DEFAULT_FIRST_WASABI2_BLOCK
    joinmarket_detector: str = DEFAULT_JOINMARKET_DETECTOR
    joinmarket_min_base_fee: int = DEFAULT_JOINMARKET_MIN_BASE_FEE
    joinmarket_percentage_fee: float = DEFAULT_JOINMARKET_PERCENTAGE_FEE
    joinmarket_max_depth: int = DEFAULT_JOINMARKET_MAX_DEPTH

    @classmethod
    def from_args(cls, args: argparse.Namespace, first_wasabi2_block: int = DEFAULT_FIRST_WASABI2_BLOCK) -> Detector:
        return cls(
            coinjoin_type=args.coinjoin_type,
            min_input_count=args.min_input_count,
            first_wasabi2_block=first_wasabi2_block,
            joinmarket_detector=args.joinmarket_detector,
            joinmarket_min_base_fee=args.joinmarket_min_base_fee,
            joinmarket_percentage_fee=args.joinmarket_percentage_fee,
            joinmarket_max_depth=args.joinmarket_max_depth,
        )

    def artifact_parameters(self) -> DetectorParameters:
        """The options an analysis artifact records; a report must request the same ones."""
        return {
            "coinjoin_type": self.coinjoin_type,
            "min_input_count": self.min_input_count,
            "joinmarket_detector": self.joinmarket_detector,
            "joinmarket_min_base_fee": self.joinmarket_min_base_fee,
            "joinmarket_percentage_fee": self.joinmarket_percentage_fee,
            "joinmarket_max_depth": self.joinmarket_max_depth,
        }

    def manifest(self) -> DetectorManifest:
        """Only the selected CoinJoin type's parameters."""
        detector: DetectorManifest = {
            "coinjoin_type": self.coinjoin_type,
            "blocksci_min_input_count": self.min_input_count,
        }
        if self.coinjoin_type == "wasabi2":
            detector["first_wasabi2_block"] = self.first_wasabi2_block
        if self.coinjoin_type == "joinmarket":
            detector.update(
                {
                    "joinmarket_detector": self.joinmarket_detector,
                    "joinmarket_min_base_fee": self.joinmarket_min_base_fee,
                    "joinmarket_percentage_fee": self.joinmarket_percentage_fee,
                    "joinmarket_max_depth": self.joinmarket_max_depth,
                }
            )
        return detector


@dataclass(frozen=True)
class ReportImages:
    """Image references by component, and the digest each one was pinned or inspected at."""

    references: dict[str, str | None] = field(default_factory=dict)
    digests: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> ReportImages:
        # The uploader and report images have no digest option; a pinned reference carries its own.
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
        "uploader": values.get("uploader"),
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
    parser.add_argument(
        "--joinmarket-detector",
        choices=("possible", "definite"),
        default=os.environ.get("BLOCKSCI_JOINMARKET_DETECTOR", DEFAULT_JOINMARKET_DETECTOR),
        help="JoinMarket detector to use for --coinjoin-type joinmarket.",
    )
    parser.add_argument(
        "--joinmarket-min-base-fee",
        type=int,
        default=os.environ.get("BLOCKSCI_JOINMARKET_MIN_BASE_FEE", DEFAULT_JOINMARKET_MIN_BASE_FEE),
        help="Minimum base fee passed to the BlockSci JoinMarket detector.",
    )
    parser.add_argument(
        "--joinmarket-percentage-fee",
        type=float,
        default=os.environ.get("BLOCKSCI_JOINMARKET_PERCENTAGE_FEE", DEFAULT_JOINMARKET_PERCENTAGE_FEE),
        help="Percentage fee passed to the BlockSci JoinMarket detector.",
    )
    parser.add_argument(
        "--joinmarket-max-depth",
        type=int,
        default=os.environ.get("BLOCKSCI_JOINMARKET_MAX_DEPTH", DEFAULT_JOINMARKET_MAX_DEPTH),
        help="Maximum subset-search depth passed to the BlockSci JoinMarket detector.",
    )


def _environment_height(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else None


def add_window_arguments(parser: argparse.ArgumentParser) -> None:
    """Heights an external-chain analysis may see and scan; the whole index by default."""
    parser.add_argument(
        "--pin-height",
        type=int,
        default=_environment_height("BLOCKSCI_PIN_HEIGHT"),
        help="Last height the analysis opens the index at (Blockchain(config, pin + 1)).",
    )
    parser.add_argument(
        "--scan-start-height",
        type=int,
        default=_environment_height("SCAN_START_HEIGHT"),
        help="First height scanned.",
    )
    parser.add_argument(
        "--scan-end-height",
        type=int,
        default=_environment_height("SCAN_END_HEIGHT"),
        help="First height not scanned.",
    )


def add_image_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--blocksci-image", default=os.environ.get("BLOCKSCI_IMAGE"))
    parser.add_argument("--coinjoin-analysis-image", default=os.environ.get("COINJOIN_ANALYSIS_IMAGE"))
    parser.add_argument("--coinjoin-emulator-image", default=os.environ.get("COINJOIN_EMULATOR_IMAGE"))
    parser.add_argument("--uploader-image", default=os.environ.get("COINJOIN_UPLOADER_IMAGE"))
    parser.add_argument("--unified-report-image", default=os.environ.get("COINJOIN_UNIFIED_REPORT_IMAGE"))
    parser.add_argument("--blocksci-image-digest", default=os.environ.get("BLOCKSCI_IMAGE_DIGEST"))
    parser.add_argument("--blocksci-image-id", default=os.environ.get("BLOCKSCI_IMAGE_ID"))
    parser.add_argument(
        "--coinjoin-analysis-image-digest",
        default=os.environ.get("COINJOIN_ANALYSIS_IMAGE_DIGEST"),
    )
    parser.add_argument("--coinjoin-analysis-image-id", default=os.environ.get("COINJOIN_ANALYSIS_IMAGE_ID"))
    parser.add_argument("--coinjoin-emulator-image-digest", default=os.environ.get("COINJOIN_EMULATOR_IMAGE_DIGEST"))
    parser.add_argument("--coinjoin-emulator-image-id", default=os.environ.get("COINJOIN_EMULATOR_IMAGE_ID"))
