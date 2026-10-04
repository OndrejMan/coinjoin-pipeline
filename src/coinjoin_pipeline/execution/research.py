#!/usr/bin/env python3
"""EXPERIMENTAL: Host-side researcher commands used by the coinjoin-pipeline CLI.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import get_args

from exporters.artifact_paths import BASELINE_FILE, FALSE_CJTXS_FILE, REPORT_FILE, REPORT_MARKDOWN_FILE
from exporters.common import load_json

from coinjoin_pipeline.configuration import ContainerRuntime
from coinjoin_pipeline.execution.locks import acquire_lock
from coinjoin_pipeline.execution.pbs.commands import detector_arguments
from coinjoin_pipeline.execution.run_catalog import (
    create_external_manifest,
    discover_runs,
    load_manifest,
    mode_for_run,
    report_status,
    stage_state,
    write_manifest,
)
from coinjoin_pipeline.execution.scenarios import (
    packaged_scenarios,
    resolve_scenario,
    validate_scenario,
)
from coinjoin_pipeline.execution.settings import DEFAULT_BLOCKSCI_IMAGE
from coinjoin_pipeline.paths import PIPELINE_ROOT
from coinjoin_pipeline.storage.s3 import validate_run_id

ROOT = PIPELINE_ROOT
DEFAULT_RUNS_ROOT = Path(os.environ.get("EMULATION_LOGS_DIR", ROOT.parent / "coinjoin-runs")).expanduser()


class ExitError(ValueError):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def require_datadir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not (resolved / "blocks").is_dir():
        raise ValueError(f"Bitcoin Core datadir must contain blocks/: {resolved}")
    return resolved


def require_baseline(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"Baseline file not found: {resolved}")
    manifest = load_json(resolved)
    if not isinstance(manifest.get("coinjoins"), dict):
        raise ValueError(f"Baseline must contain a top-level 'coinjoins' object: {resolved}")
    return resolved


def require_false_cjtxs(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"False-positive file not found: {resolved}")
    data = load_json(resolved)
    if any(not isinstance(values, list) for values in data.values()):
        raise ValueError(f"False-positive file values must all be lists: {resolved}")
    return resolved


def resolve_false_cjtxs(baseline: Path, configured: list[Path] | None) -> list[Path]:
    candidates = configured if configured is not None else sorted(baseline.parent.glob("false_cjtxs.json*"))
    return [require_false_cjtxs(path) for path in candidates]


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
    if mode_for_run(run_dir, manifest) == "external":
        print(f"  ./runIt.sh external analyze --run-id {run_dir.name} --resume")


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


def print_scenarios(engine: str | None) -> None:
    scenarios = packaged_scenarios(engine)
    print("name\tengine\trounds\twallets\tmakers\ttakers\tpath")
    for item in scenarios:
        print(
            f"{item['name']}\t{item['engine']}\t{item['rounds']}\t{item['wallet_count']}\t"
            f"{item['makers']}\t{item['takers']}\t{item['path']}"
        )


def show_or_validate_scenario(value: str, engine: str, show: bool) -> None:
    summary = validate_scenario(resolve_scenario(value), engine)
    if show:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"VALID: {summary['name']} ({engine})")


def external_analyze(args: argparse.Namespace) -> None:
    runs_root = args.runs_root.expanduser().resolve()
    validate_run_id(args.run_id)
    run_dir = runs_root / args.run_id
    if run_dir.exists() and not args.resume:
        raise FileExistsError(f"Run already exists: {run_dir}; pass --resume to reuse it.")
    if args.resume and not run_dir.is_dir():
        raise ValueError(f"Cannot resume missing run: {run_dir}")
    baseline_target = run_dir / BASELINE_FILE
    baseline = baseline_target
    if args.resume:
        manifest = load_manifest(run_dir)
        inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), dict) else {}
        source_datadir = inputs.get("bitcoin_datadir") if isinstance(inputs, dict) else None
        if not source_datadir:
            raise ValueError("Cannot resume: external manifest has no bitcoin_datadir provenance")
        datadir = require_datadir(Path(str(source_datadir)))
        if not baseline_target.is_file():
            raise ValueError(f"Cannot resume: baseline is missing at {baseline_target}")
        false_cjtxs: list[Path] = []
    else:
        assert args.bitcoin_datadir is not None
        assert args.baseline is not None
        datadir = require_datadir(args.bitcoin_datadir)
        baseline = require_baseline(args.baseline)
        false_cjtxs = resolve_false_cjtxs(baseline, getattr(args, "false_cjtxs", None))

    storage_root = (
        run_dir if args.resume else next(parent for parent in (runs_root, *runs_root.parents) if parent.exists())
    )
    available_gb = shutil.disk_usage(storage_root).free // (1024**3)
    if available_gb < args.min_free_gb:
        raise ValueError(
            f"Only {available_gb} GiB free at {run_dir}; require at least {args.min_free_gb} GiB "
            "for a persistent BlockSci index."
        )

    runs_root.mkdir(parents=True, exist_ok=True)
    _lock = acquire_lock(runs_root / f".{args.run_id}.lock")
    if not args.resume:
        run_dir.mkdir()
        baseline_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(baseline, baseline_target)
        for index, source in enumerate(false_cjtxs):
            target = run_dir / FALSE_CJTXS_FILE
            if index:
                target = target.with_name(f"false_cjtxs.json.{index}")
            shutil.copy2(source, target)
        write_manifest(
            run_dir,
            create_external_manifest(
                run_dir,
                datadir,
                baseline,
                args.network,
                args.coinjoin_type,
                false_cjtxs,
            ),
        )

    exporter_dir = ROOT / "exporters"
    image = args.blocksci_image
    reproduction_command = f"./runIt.sh external analyze --run-id {shlex.quote(args.run_id)} --resume"
    command = [
        args.runtime,
        "run",
        "--rm",
        "--env",
        f"REPRODUCTION_COMMAND={reproduction_command}",
        "-v",
        f"{datadir}:/mnt/data:ro",
        "-v",
        f"{runs_root}:/runs:rw",
        "-v",
        f"{exporter_dir}:/mnt/exporters:ro",
        image,
        "/bin/bash",
        "-lc",
        external_command(args),
    ]
    print("Running external BlockSci analysis; parsed data remains in the selected run directory.")
    subprocess.run(command, check=True)
    print(f"Report: {run_dir / REPORT_MARKDOWN_FILE}")


def dry_run_external(args: argparse.Namespace) -> None:
    validate_run_id(args.run_id)
    runs_root = args.runs_root.expanduser().resolve()
    run_dir = runs_root / args.run_id
    if args.resume:
        manifest = load_manifest(run_dir)
        inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), dict) else {}
        source_datadir = inputs.get("bitcoin_datadir") if isinstance(inputs, dict) else None
        if not source_datadir:
            raise ValueError("Cannot resume: external manifest has no bitcoin_datadir provenance")
        require_datadir(Path(str(source_datadir)))
        if not (run_dir / BASELINE_FILE).is_file():
            raise ValueError(f"Cannot resume: baseline is missing at {run_dir / BASELINE_FILE}")
    else:
        if run_dir.exists():
            raise FileExistsError(f"Run already exists: {run_dir}; pass --resume to reuse it.")
        assert args.bitcoin_datadir is not None
        assert args.baseline is not None
        require_datadir(args.bitcoin_datadir)
        baseline = require_baseline(args.baseline)
        resolve_false_cjtxs(baseline, getattr(args, "false_cjtxs", None))
    runtime_check(args.runtime)
    print("[dry-run] No run directory, baseline copy, BlockSci index, container, or report will be created.")
    print(f"[dry-run] external run: {run_dir}")
    print(f"[dry-run] command: {external_command(args)}")


def external_command(args: argparse.Namespace) -> str:
    return shlex.join(
        [
            "python3",
            "/mnt/exporters/worker.py",
            "run",
            "--run-dir",
            f"/runs/{args.run_id}",
            "--mode",
            "external",
            "--network",
            args.network,
            "--disk",
            "/mnt/data",
            *args.detector_arguments,
            "--skip-clustering",
            "--report",
        ]
    )


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Researcher-facing run catalog.")
    cli.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    cli.add_argument("--runtime", choices=get_args(ContainerRuntime), default="docker")
    subcommands = cli.add_subparsers(dest="command", required=True)
    runs = subcommands.add_parser("runs")
    run_commands = runs.add_subparsers(dest="runs_command", required=True)
    run_commands.add_parser("list")
    inspect = run_commands.add_parser("inspect")
    inspect.add_argument("--run-dir", required=True)
    validate = run_commands.add_parser("validate")
    validate.add_argument("--run-dir", required=True)
    validate.add_argument("--blocksci-image")
    scenarios = subcommands.add_parser("scenarios")
    scenario_commands = scenarios.add_subparsers(dest="scenarios_command", required=True)
    list_scenarios = scenario_commands.add_parser("list")
    list_scenarios.add_argument("--engine", choices=("wasabi", "joinmarket"))
    for name in ("show", "validate"):
        scenario_command = scenario_commands.add_parser(name)
        scenario_command.add_argument("scenario")
        scenario_command.add_argument("--engine", choices=("wasabi", "joinmarket"), required=True)
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
        elif args.scenarios_command == "list":
            print_scenarios(args.engine)
        else:
            show_or_validate_scenario(args.scenario, args.engine, args.scenarios_command == "show")
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


def run_external_configuration(config):
    """Adapt the research operation to the common resolved configuration."""
    args = argparse.Namespace(
        runs_root=Path(config.runs_root),
        run_id=config.run_id,
        runtime=config.runtime,
        coinjoin_type=config.coinjoin_type,
        detector_arguments=detector_arguments(config),
        resume=config.external.resume,
        dry_run=config.dry_run,
        bitcoin_datadir=Path(config.external.bitcoin_datadir) if config.external.bitcoin_datadir else None,
        baseline=Path(config.external.baseline) if config.external.baseline else None,
        false_cjtxs=[Path(path) for path in config.external.false_cjtxs] or None,
        network=config.external.network or "bitcoin",
        min_free_gb=config.external.min_free_gb if config.external.min_free_gb is not None else 20,
        blocksci_image=config.images.blocksci,
    )
    if config.dry_run:
        dry_run_external(args)
    else:
        external_analyze(args)
