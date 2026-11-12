#!/usr/bin/env python3
"""The analysis payload the BlockSci container runs."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporters.artifact_paths import BLOCKSCI_CONFIG_FILE, REPORT_JSON
from exporters.normalization import block_height_from_path, exported_block_paths
from exporters.parameters import add_detector_arguments


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("run", "parse", "analyze", "report"))
    run_id = os.environ.get("ACTIVE_RUN_ID")
    default_run = Path("/runs/emulation/logs") / run_id if run_id else None
    parser.add_argument("--run-dir", type=Path, default=default_run)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--disk", type=Path, default=Path("/mnt/data/regtest"))
    parser.add_argument("--network", default="bitcoin_regtest")
    parser.add_argument("--max-block", type=int)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--share-output", action="store_true")
    parser.add_argument("--blocksci-analysis", type=Path)
    parser.add_argument("--output-name", default=REPORT_JSON)
    add_detector_arguments(parser)
    args = parser.parse_args(argv)
    if args.run_dir is None:
        parser.error("--run-dir or ACTIVE_RUN_ID is required")
    args.run_dir = args.run_dir.expanduser().resolve()
    args.config = args.config or args.run_dir / BLOCKSCI_CONFIG_FILE
    args.runs_root = args.run_dir.parent
    return args


def parse_chain(args):
    args.config.parent.mkdir(parents=True, exist_ok=True)
    maximum = args.max_block
    if maximum is None:
        heights = [
            height for height in map(block_height_from_path, exported_block_paths(args.run_dir)) if height is not None
        ]
        if not heights:
            raise ValueError("No exported block height is available for BlockSci parsing")
        maximum = max(heights) + 1
    command = [
        "blocksci_parser",
        str(args.config),
        "generate-config",
        args.network,
        str(args.config.parent / "parsed"),
        "--disk",
        str(args.disk),
    ]
    command.extend(["--max-block", str(maximum)])
    subprocess.run(command, check=True)
    subprocess.run(["blocksci_parser", str(args.config), "update"], check=True)
    if args.share_output:
        # A root-owned Docker parser otherwise leaves its 0700 output unreadable to the host.
        subprocess.run(["chmod", "-R", "a+rX", str(args.config.parent)], check=True)


def execute(args):
    if args.phase in {"run", "parse"}:
        parse_chain(args)
    if args.phase in {"run", "analyze"}:
        from exporters.blocksci_export.analysis import write_analysis

        write_analysis(args)
    if args.phase == "report" or args.report:
        from exporters.cli import assemble_report

        assemble_report(args)


def main(argv=None):
    execute(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
