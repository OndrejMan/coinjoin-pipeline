#!/usr/bin/env python3
"""EXPERIMENTAL: Host-side researcher commands used by the coinjoin-pipeline CLI.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import get_args

from exporters.artifact_paths import REPORT_FILE
from exporters.common import load_json

from coinjoin_pipeline.configuration import ContainerRuntime
from coinjoin_pipeline.execution.run_catalog import (
    discover_runs,
    load_manifest,
    mode_for_run,
    report_status,
    stage_state,
)
from coinjoin_pipeline.execution.settings import DEFAULT_BLOCKSCI_IMAGE
from coinjoin_pipeline.paths import PIPELINE_ROOT, default_runs_root

ROOT = PIPELINE_ROOT


class ExitError(ValueError):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def print_runs(runs_root: Path) -> None:
    states = discover_runs(runs_root)
    if not states:
        print(f"No runs found under {runs_root}")
        return
    print("run_id\tmode\tstages\treport_status\tartifact_path")
    for state in states:
        completed = ",".join(name for name, status in state.stages.items() if status == "present") or "none"
        print(f"{state.run_dir.name}\t{state.mode}\t{completed}\t{state.report_status}\t{state.run_dir}")


def inspect_run(runs_root: Path, run_id: str) -> None:
    run_dir = (runs_root / run_id).resolve()
    if not run_dir.is_dir():
        raise ValueError(f"Run directory not found: {run_dir}")
    manifest = load_manifest(run_dir)
    print(f"Run: {run_dir.name}")
    print(f"Mode: {mode_for_run(run_dir, manifest)}")
    status = report_status(run_dir)
    print("Stages:")
    for name, stage in stage_state(run_dir, status).items():
        print(f"  {name}: {stage}")
    print(f"Report status: {status}")
    if manifest:
        print("Manifest:")
        print(json.dumps(manifest, indent=2, sort_keys=True))
    print("Resume:")
    print(f"  ./runIt.sh export --run-dir {run_dir.name}")


def runtime_check(runtime: str) -> None:
    if shutil.which(runtime) is None:
        raise ValueError(f"{runtime} CLI is not installed or not on PATH")
    try:
        subprocess.run(
            [runtime, "info"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError(f"{runtime} daemon/API is unavailable: {error.stderr.strip()}") from error


def validate_existing_run(args: argparse.Namespace) -> None:
    runs_root = args.runs_root.expanduser().resolve()
    run_dir = (runs_root / args.run_dir).resolve()
    if not run_dir.is_dir():
        raise ExitError(f"Run directory not found: {run_dir}", 3)
    if mode_for_run(run_dir) != "emulator":
        raise ValueError("Container-backed validation currently applies only to emulator runs")
    status = report_status(run_dir)
    stages = stage_state(run_dir, status)
    missing = [name for name in ("emulation", "baseline", "blocksci", "report") if stages[name] == "missing"]
    if missing:
        raise ExitError(f"Run is incomplete; missing stages: {', '.join(missing)}", 3)
    if status != "complete":
        raise ExitError(f"Report validation failed: {status}", 4)
    report = load_json(run_dir / REPORT_FILE)
    run_manifest = report.get("run_manifest")
    images = run_manifest.get("images", {}) if isinstance(run_manifest, dict) else {}
    image = args.blocksci_image or (images.get("blocksci") if isinstance(images, dict) else None)
    image = image or DEFAULT_BLOCKSCI_IMAGE
    runtime_check(args.runtime)
    blocksci_check = (
        "import blocksci; "
        "chain=blocksci.Blockchain("
        f"'/runs/emulation/logs/{args.run_dir}/blocksci_data/config.json'"
        "); print('BlockSci chain height:', len(chain))"
    )
    command = [
        args.runtime,
        "run",
        "--rm",
        "-v",
        f"{runs_root}:/runs/emulation/logs:ro",
        str(image),
        "python3",
        "-c",
        blocksci_check,
    ]
    subprocess.run(command, check=True)
    print(f"VALID: {run_dir.name} (runtime={args.runtime}, report={status})")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="cjp", description="Researcher-facing run catalog.")
    cli.add_argument("--runs-root", type=Path, default=default_runs_root(), help="Runs root (default: %(default)s).")
    cli.add_argument("--runtime", choices=get_args(ContainerRuntime), default="docker")
    subcommands = cli.add_subparsers(dest="command", required=True)
    runs = subcommands.add_parser("runs", help="List, inspect and validate run directories.")
    run_commands = runs.add_subparsers(dest="runs_command", required=True)
    run_commands.add_parser("list", help="Every run with its mode, stages and report status.")
    inspect = run_commands.add_parser("inspect", help="One run's manifest, stages and report status.")
    inspect.add_argument("--run-dir", required=True, help="Run directory, absolute or below the runs root.")
    validate = run_commands.add_parser("validate", help="Check one run's report inputs in the BlockSci image.")
    validate.add_argument("--run-dir", required=True, help="Run directory, absolute or below the runs root.")
    validate.add_argument("--blocksci-image", help="BlockSci image to validate with.")
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "runs":
            if args.runs_command == "list":
                print_runs(args.runs_root)
            elif args.runs_command == "inspect":
                inspect_run(args.runs_root, args.run_dir)
            else:
                validate_existing_run(args)
    except ExitError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return error.code
    except subprocess.CalledProcessError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 5
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
