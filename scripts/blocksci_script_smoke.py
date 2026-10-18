#!/usr/bin/env python3
"""Smoke-test the ``--blocksci-task script`` PBS path on a regtest run.

The job binds this file as /mnt/user-analysis.py inside the BlockSci image
(Python 3.8) and runs it over the run's parsed index. It prints a short summary
and writes summary.json to BLOCKSCI_OUTPUT_DIR, which the job uploads to
<run>/blocksci-custom-analysis_data/ next to a copy of this script.
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path

import blocksci


def optional_int(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else None


def tx_record(tx) -> dict:
    input_sats = int(sum(inp.value for inp in tx.inputs))
    output_sats = int(sum(out.value for out in tx.outputs))
    return {
        "txid": str(tx.hash),
        "block_height": int(tx.block_height),
        "input_count": int(tx.input_count),
        "output_count": int(tx.output_count),
        "input_sats": input_sats,
        "output_sats": output_sats,
        "fee_sats": input_sats - output_sats,
    }


def detect(chain, coinjoin_type: str, min_input_count: int | None) -> list:
    if coinjoin_type == "joinmarket":
        txes, _skipped = chain.scan_coinjoins_by_subset_matching(
            0,
            len(chain),
            os.environ["JOINMARKET_DETECTOR"],
            int(os.environ["JOINMARKET_MIN_BASE_FEE"]),
            float(os.environ["JOINMARKET_PERCENTAGE_FEE"]),
            int(os.environ["JOINMARKET_MAX_DEPTH"]),
        )
        return list(txes)
    if min_input_count is None:
        return list(chain.filter_coinjoin_txes_raw(0, len(chain), coinjoin_type))
    return list(chain.filter_coinjoin_txes_raw(0, len(chain), coinjoin_type, min_input_count))


def main() -> None:
    started = time.time()
    config = os.environ["BLOCKSCI_CONFIG"]
    output_dir = Path(os.environ["BLOCKSCI_OUTPUT_DIR"])
    coinjoin_type = os.environ.get("COINJOIN_TYPE", "wasabi2")
    min_input_count = optional_int("MIN_INPUT_COUNT")

    chain = blocksci.Blockchain(config)
    tx_count = sum(len(block.txes) for block in chain)
    coinjoins = sorted(
        (tx_record(tx) for tx in detect(chain, coinjoin_type, min_input_count)), key=lambda r: r["block_height"]
    )

    summary = {
        "run_id": os.environ.get("ACTIVE_RUN_ID"),
        "python": platform.python_version(),
        "config": config,
        "coinjoin_type": coinjoin_type,
        "min_input_count": min_input_count,
        "block_count": len(chain),
        "tx_count": tx_count,
        "coinjoin_count": len(coinjoins),
        "coinjoin_input_sats": sum(r["input_sats"] for r in coinjoins),
        "elapsed_seconds": round(time.time() - started, 3),
        "coinjoins": coinjoins,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"[blocksci-script-smoke] {summary['run_id']}: {summary['block_count']} blocks, "
        f"{tx_count} txs, {len(coinjoins)} {coinjoin_type} coinjoins "
        f"(min_input_count={min_input_count}) in {summary['elapsed_seconds']} s"
    )


if __name__ == "__main__":
    main()
