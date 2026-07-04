"""Shared command-line contracts for pipeline and research entry points."""

from __future__ import annotations

import argparse

COINJOIN_TYPES = ("wasabi2", "joinmarket")
DEFAULT_COINJOIN_TYPE = "wasabi2"
CONTAINER_RUNTIMES = ("docker", "podman")


def add_coinjoin_type_argument(parser: argparse.ArgumentParser) -> None:
    """Add the CoinJoin heuristic selector shared by all analysis workflows."""
    parser.add_argument(
        "--coinjoin-type",
        choices=COINJOIN_TYPES,
        default=DEFAULT_COINJOIN_TYPE,
        help=f"BlockSci CoinJoin heuristic family (default: {DEFAULT_COINJOIN_TYPE}).",
    )


def add_runtime_argument(
    parser: argparse.ArgumentParser,
    *,
    default: str | object = argparse.SUPPRESS,
    help_text: str | None = argparse.SUPPRESS,
) -> None:
    """Add the host container-runtime selector."""
    parser.add_argument(
        "--runtime",
        choices=CONTAINER_RUNTIMES,
        default=default,
        help=help_text,
    )


def add_engine_argument(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    """Add the emulator engine selector."""
    parser.add_argument(
        "--engine",
        choices=("wasabi", "joinmarket"),
        required=required,
        help="CoinJoin emulator engine to run. Select explicitly: wasabi or joinmarket.",
    )


def add_dry_run_argument(parser: argparse.ArgumentParser) -> None:
    """Add the common side-effect-free preview switch."""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected pipeline stages and exit without side effects.",
    )
