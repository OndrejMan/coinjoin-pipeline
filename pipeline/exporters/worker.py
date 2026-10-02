#!/usr/bin/env python3
"""The same analysis payload runs under Docker and Apptainer."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporters.artifact_paths import BLOCKSCI_CONFIG_FILE, EXPORTED_BLOCKS_DIR, REPORT_JSON
from exporters.parameters import add_detector_arguments, add_image_arguments


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("run", "parse", "update", "analyze", "report"))
    run_id = os.environ.get("PBS_RUN_ID") or os.environ.get("ACTIVE_RUN_ID")
    default_run = Path("/runs/emulation/logs") / run_id if run_id else None
    parser.add_argument("--run-dir", type=Path, default=default_run)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--disk", type=Path, default=Path("/mnt/data/regtest"))
    parser.add_argument("--network", default="bitcoin_regtest")
    parser.add_argument("--max-block", type=int)
    parser.add_argument("--script", type=Path, default=os.environ.get("BLOCKSCI_SCRIPT") or None)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--share-output", action="store_true")
    parser.add_argument("--mode", choices=("emulator", "external"), default="emulator")
    parser.add_argument("--skip-clustering", action="store_true")
    parser.add_argument("--cluster-output-dir", type=Path)
    parser.add_argument("--scenario", type=Path, default=os.environ.get("SCENARIO_FALLBACK_PATH") or None)
    parser.add_argument("--engine", default=os.environ.get("COINJOIN_ENGINE"))
    parser.add_argument("--blocksci-analysis", type=Path)
    parser.add_argument("--output-name", default=REPORT_JSON)
    parser.add_argument("--emulator-git-commit", default=os.environ.get("COINJOIN_EMULATOR_GIT_COMMIT"))
    add_detector_arguments(parser)
    add_image_arguments(parser)
    args = parser.parse_args(argv)
    if args.run_dir is None:
        parser.error("--run-dir or ACTIVE_RUN_ID/PBS_RUN_ID is required")
    args.run_dir = args.run_dir.expanduser().resolve()
    args.config = args.config or args.run_dir / BLOCKSCI_CONFIG_FILE
    args.runs_root = args.run_dir.parent
    args.markdown = True
    return args


def parse_chain(args):
    args.config.parent.mkdir(parents=True, exist_ok=True)
    if args.phase != "update" and not (args.mode == "external" and args.config.is_file()):
        maximum = args.max_block
        if maximum is None and args.mode != "external":
            blocks = args.run_dir / EXPORTED_BLOCKS_DIR
            heights = [
                int(path.stem[len("block_") :])
                for path in blocks.glob("block_*.json")
                if path.stem[len("block_") :].isdigit()
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
        if maximum is not None:
            command.extend(["--max-block", str(maximum)])
        subprocess.run(command, check=True)
    subprocess.run(["blocksci_parser", str(args.config), "update"], check=True)
    if args.share_output:
        # A root-owned Docker parser otherwise leaves its 0700 output unreadable to the host.
        subprocess.run(["chmod", "-R", "a+rX", str(args.config.parent)], check=True)


def execute(args):
    if args.phase in {"run", "parse", "update"}:
        parse_chain(args)
    if args.phase == "run" and args.script:
        environment = {
            **os.environ,
            "ACTIVE_RUN_ID": args.run_dir.name,
            "BLOCKSCI_CONFIG": str(args.config),
            "BLOCKSCI_RUN_DIR": str(args.run_dir),
        }
        subprocess.run([sys.executable, str(args.script)], check=True, cwd=args.run_dir, env=environment)
    if args.phase in {"run", "analyze"}:
        from exporters.blocksci_export.analysis import write_analysis

        write_analysis(args)
    if args.phase == "report" or args.report:
        from exporters.cli import assemble_report

        if args.mode == "emulator":
            args.network = None
        assemble_report(args)


def main(argv=None):
    execute(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
