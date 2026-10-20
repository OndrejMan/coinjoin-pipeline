#!/usr/bin/env python3
"""Scan a BlockSci mainnet chain for CoinJoin transactions.

This script is intentionally independent from the regtest emulator report
pipeline. It is for MetaCentrum/full-chain feasibility evidence, where there is
no emulator ground truth and therefore no unified_report.json comparison.

Every detected transaction goes to a JSON Lines file next to the summary, and
the counts and daily/monthly aggregates are computed over all of them; the
summary itself only carries a preview. Runs inside the BlockSci image, so the
syntax stays Python 3.8.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

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


def resolve_range(start: int, end: int | None, blocks: int) -> tuple[int, int]:
    """Half-open [start, end) within the opened chain; end defaults to its last block."""
    end = blocks if end is None else end
    if not 0 <= start < end <= blocks:
        raise ValueError(f"scan range [{start}, {end}) is outside the chain of {blocks} blocks")
    return start, end


def write_records(txes: Iterable[Any], path: Path, preview_limit: int) -> dict[str, Any]:
    """Stream every record to `path` and aggregate over all of them."""
    count = 0
    daily: Counter[str] = Counter()
    monthly: Counter[str] = Counter()
    inputs = 0
    preview = []
    with path.open("w", encoding="utf-8") as handle:
        for tx in txes:
            record = tx_record(tx)
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            count += 1
            inputs += record["input_count"]
            if record["block_time"]:
                daily[record["block_time"][:10]] += 1
                monthly[record["block_time"][:7]] += 1
            if len(preview) < preview_limit:
                preview.append(record)
    return {
        "detected_count": count,
        "input_count": inputs,
        "daily": dict(sorted(daily.items())),
        "monthly": dict(sorted(monthly.items())),
        "records_file": path.name,
        "records_preview": preview,
        "records_preview_truncated": count > len(preview),
    }


def open_chain(config: Path, max_block: int | None) -> Any:
    # Blockchain(config, n) shows only blocks [0, n), so an index updated in
    # place later still answers this scan the same way.
    if max_block is None:
        return blocksci.Blockchain(str(config))
    return blocksci.Blockchain(str(config), max_block)


def scan(args: argparse.Namespace) -> dict[str, Any]:
    chain = open_chain(args.config, args.max_block)
    blocks = len(chain)
    start_height, end_height = resolve_range(args.start_height, args.end_height, blocks)
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

    records = write_records(txes, records_path(args.output), args.max_records)
    elapsed_seconds = round(time.time() - started_at, 3)

    return {
        # "partial": the JoinMarket search gave up on some candidates, so the
        # count is a lower bound rather than a complete scan.
        "status": "partial" if skipped_txids else "complete",
        "config": str(args.config),
        "index": {
            "blocks": blocks,
            "last_height": blocks - 1,
            "last_block_hash": str(chain[blocks - 1].hash),
            "max_block": args.max_block,
        },
        "scan_range": {
            "start_height": start_height,
            "end_height": end_height,
            "end_exclusive": True,
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
        "elapsed_seconds": elapsed_seconds,
        **records,
    }


def records_path(output: Path) -> Path:
    return output.with_name(output.stem + ".records.jsonl")


def environment_int(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else None


def environment_float(name: str) -> float | None:
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config_value = os.environ.get("BLOCKSCI_CONFIG")
    output_directory = os.environ.get("BLOCKSCI_OUTPUT_DIR")
    joinmarket_min_base_fee = environment_int("JOINMARKET_MIN_BASE_FEE")
    joinmarket_percentage_fee = environment_float("JOINMARKET_PERCENTAGE_FEE")
    joinmarket_max_depth = environment_int("JOINMARKET_MAX_DEPTH")
    start_height = environment_int("SCAN_START_HEIGHT")
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
        default=(Path(output_directory) / "joinmarket-mainnet-summary.json" if output_directory else None),
        required=output_directory is None,
        help=("Output JSON path; defaults to BLOCKSCI_OUTPUT_DIR/joinmarket-mainnet-summary.json."),
    )
    parser.add_argument("--coinjoin-type", default=os.environ.get("COINJOIN_TYPE", "joinmarket"))
    parser.add_argument(
        "--max-block",
        type=int,
        default=environment_int("BLOCKSCI_MAX_BLOCK"),
        help="Open the index pinned to blocks [0, MAX_BLOCK); defaults to BLOCKSCI_MAX_BLOCK, else the whole index.",
    )
    parser.add_argument(
        "--start-height",
        type=int,
        default=0 if start_height is None else start_height,
        help="First height scanned; defaults to SCAN_START_HEIGHT, else 0.",
    )
    parser.add_argument(
        "--end-height",
        type=int,
        default=environment_int("SCAN_END_HEIGHT"),
        help="First height not scanned; defaults to SCAN_END_HEIGHT, else the end of the opened chain.",
    )
    parser.add_argument("--min-input-count", type=int, default=environment_int("MIN_INPUT_COUNT"))
    parser.add_argument(
        "--max-records",
        type=int,
        default=10000,
        help="Records copied into the summary as a preview; the JSON Lines file holds all of them.",
    )
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
        default=(0.00004 if joinmarket_percentage_fee is None else joinmarket_percentage_fee),
    )
    parser.add_argument(
        "--joinmarket-max-depth",
        type=int,
        default=200000 if joinmarket_max_depth is None else joinmarket_max_depth,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = scan(args)
    except Exception as error:
        # A failed scan still leaves a summary, so nobody mistakes a missing
        # result for zero detections.
        failed = {"status": "failed", "error": f"{type(error).__name__}: {error}", "coinjoin_type": args.coinjoin_type}
        args.output.write_text(json.dumps(failed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Scan failed: {failed['error']}")
        return 1
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    print(f"Detected {report['detected_count']} {args.coinjoin_type} transactions ({report['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
