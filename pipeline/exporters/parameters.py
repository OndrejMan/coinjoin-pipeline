"""Detector and image options shared by analysis and report input boundaries."""

from __future__ import annotations

import argparse
import os

from exporters.common import (
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
)


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


def add_image_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--blocksci-image", default=os.environ.get("BLOCKSCI_IMAGE"))
    parser.add_argument("--coinjoin-analysis-image", default=os.environ.get("COINJOIN_ANALYSIS_IMAGE"))
    parser.add_argument(
        "--coinjoin-emulator-image",
        default=os.environ.get("COINJOIN_EMULATOR_IMAGE") or os.environ.get("EMULATOR_IMAGE"),
    )
    parser.add_argument("--uploader-image", default=os.environ.get("COINJOIN_UPLOADER_IMAGE"))
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
