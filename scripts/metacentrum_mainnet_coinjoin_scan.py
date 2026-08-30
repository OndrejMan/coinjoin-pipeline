#!/usr/bin/env python3
"""Scan a BlockSci mainnet chain for CoinJoin transactions.

This script is intentionally independent from the regtest emulator report
pipeline. It is for MetaCentrum/full-chain feasibility evidence, where there is
no emulator ground truth and therefore no unified_report.json comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import blocksci


def tx_record(tx: Any) -> dict[str, Any]:
    return {
        "txid": str(tx.hash),
        "block_height": int(tx.block_height),
        "block_time": tx.block_time.isoformat() if getattr(tx, "block_time", None) else None,
        "input_count": int(tx.input_count),
        "output_count": int(tx.output_count),
        "total_input_sats": int(sum(inp.value for inp in tx.inputs)),
        "total_output_sats": int(sum(out.value for out in tx.outputs)),
    }


def scan(args: argparse.Namespace) -> dict[str, Any]:
    chain = blocksci.Blockchain(str(args.config))
    start_height = args.start_height
    end_height = args.end_height if args.end_height is not None else len(chain)
    started_at = time.time()

    if args.coinjoin_type == "joinmarket":
        if not hasattr(chain, "scan_coinjoins_by_subset_matching"):
            raise RuntimeError("This BlockSci build does not expose scan_coinjoins_by_subset_matching.")
        txes, skipped = chain.scan_coinjoins_by_subset_matching(
            start_height,
            end_height,
            args.joinmarket_detector,
            args.joinmarket_min_base_fee,
            args.joinmarket_percentage_fee,
            args.joinmarket_max_depth,
        )
        skipped_txids = sorted(str(tx.hash) for tx in skipped)
    elif args.min_input_count is None:
        txes = chain.filter_coinjoin_txes(start_height, end_height, args.coinjoin_type)
        skipped_txids = []
    else:
        txes = chain.filter_coinjoin_txes(
            start_height,
            end_height,
            args.coinjoin_type,
            args.min_input_count,
        )
        skipped_txids = []

    records = [tx_record(tx) for tx in txes]
    elapsed_seconds = round(time.time() - started_at, 3)

    return {
        "config": str(args.config),
        "chain_height": len(chain),
        "scan_range": {
            "start_height": start_height,
            "end_height": end_height,
        },
        "coinjoin_type": args.coinjoin_type,
        "min_input_count": args.min_input_count,
        "joinmarket": {
            "detector": args.joinmarket_detector,
            "min_base_fee": args.joinmarket_min_base_fee,
            "percentage_fee": args.joinmarket_percentage_fee,
            "max_depth": args.joinmarket_max_depth,
            "skipped_count": len(skipped_txids),
            "skipped_txids": skipped_txids[: args.max_records],
        }
        if args.coinjoin_type == "joinmarket"
        else None,
        "detected_count": len(records),
        "elapsed_seconds": elapsed_seconds,
        "records_truncated": len(records) > args.max_records,
        "records": records[: args.max_records],
    }


def environment_int(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else None


def environment_float(name: str) -> float | None:
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else None


def parse_args() -> argparse.Namespace:
    config_value = os.environ.get("BLOCKSCI_CONFIG")
    output_directory = os.environ.get("BLOCKSCI_OUTPUT_DIR")
    joinmarket_min_base_fee = environment_int("JOINMARKET_MIN_BASE_FEE")
    joinmarket_percentage_fee = environment_float("JOINMARKET_PERCENTAGE_FEE")
    joinmarket_max_depth = environment_int("JOINMARKET_MAX_DEPTH")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(config_value) if config_value else None,
        required=config_value is None,
        help="BlockSci config JSON; defaults to BLOCKSCI_CONFIG.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            Path(output_directory) / "joinmarket-mainnet-summary.json"
            if output_directory
            else None
        ),
        required=output_directory is None,
        help=(
            "Output JSON path; defaults to "
            "BLOCKSCI_OUTPUT_DIR/joinmarket-mainnet-summary.json."
        ),
    )
    parser.add_argument(
        "--coinjoin-type", default=os.environ.get("COINJOIN_TYPE", "joinmarket")
    )
    parser.add_argument("--start-height", type=int, default=0)
    parser.add_argument("--end-height", type=int)
    parser.add_argument(
        "--min-input-count", type=int, default=environment_int("MIN_INPUT_COUNT")
    )
    parser.add_argument("--max-records", type=int, default=10000)
    parser.add_argument(
        "--joinmarket-detector",
        choices=("possible", "definite"),
        default=os.environ.get("JOINMARKET_DETECTOR", "definite"),
    )
    parser.add_argument(
        "--joinmarket-min-base-fee",
        type=int,
        default=5000 if joinmarket_min_base_fee is None else joinmarket_min_base_fee,
    )
    parser.add_argument(
        "--joinmarket-percentage-fee",
        type=float,
        default=(
            0.00004
            if joinmarket_percentage_fee is None
            else joinmarket_percentage_fee
        ),
    )
    parser.add_argument(
        "--joinmarket-max-depth",
        type=int,
        default=200000 if joinmarket_max_depth is None else joinmarket_max_depth,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = scan(args)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    print(f"Detected {report['detected_count']} {args.coinjoin_type} transactions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
