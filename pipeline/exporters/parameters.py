"""Detector options shared by analysis and report input boundaries."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from exporters.common import DEFAULT_FIRST_WASABI2_BLOCK
from exporters.report_types import DetectorManifest, DetectorParameters


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
