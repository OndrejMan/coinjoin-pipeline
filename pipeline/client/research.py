#!/usr/bin/env python3
"""Host-side researcher commands used by the coinjoin-pipeline CLI."""

from __future__ import annotations

import argparse
import atexit
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.cli_options import add_coinjoin_type_argument, add_runtime_argument
from client.run_catalog import (
    BASELINE_FILE,
    FALSE_CJTXS_FILE,
    REPORT_DIR,
    create_external_manifest,
    discover_runs,
    load_manifest,
    mode_for_run,
    report_status,
    stage_state,
    write_manifest,
)
from client.scenarios import packaged_scenarios, resolve_scenario, validate_scenario

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_ROOT = Path(
    os.environ.get("EMULATION_LOGS_DIR", ROOT.parent / "coinjoin-runs")
).expanduser()


class ExitError(ValueError):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def acquire_lock(path: Path) -> object:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise ExitError(f"Another command holds lock: {path}", 2) from error
    atexit.register(handle.close)
    return handle


def require_datadir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not (resolved / "blocks").is_dir():
        raise ValueError(f"Bitcoin Core datadir must contain blocks/: {resolved}")
    return resolved


def require_baseline(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"Baseline file not found: {resolved}")
    manifest = load_manifest_json(resolved)
    if not isinstance(manifest.get("coinjoins"), dict):
        raise ValueError(f"Baseline must contain a top-level 'coinjoins' object: {resolved}")
    return resolved


def require_false_cjtxs(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"False-positive file not found: {resolved}")
    data = load_manifest_json(resolved)
    if any(not isinstance(values, list) for values in data.values()):
        raise ValueError(f"False-positive file values must all be lists: {resolved}")
    return resolved


def resolve_false_cjtxs(baseline: Path, configured: list[Path] | None) -> list[Path]:
    candidates = configured if configured is not None else sorted(baseline.parent.glob("false_cjtxs.json*"))
    return [require_false_cjtxs(path) for path in candidates]


def load_manifest_json(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def print_runs(runs_root: Path) -> None:
    states = discover_runs(runs_root)
    if not states:
        print(f"No runs found under {runs_root}")
        return
    print("run_id\tmode\tstages\treport_status\tartifact_path")
    for state in states:
        completed = ",".join(name for name, done in state.stages.items() if done) or "none"
        print(f"{state.run_dir.name}\t{state.mode}\t{completed}\t{report_status(state.run_dir)}\t{state.run_dir}")


def inspect_run(runs_root: Path, run_id: str) -> None:
    run_dir = (runs_root / run_id).resolve()
    if not run_dir.is_dir():
        raise ValueError(f"Run directory not found: {run_dir}")
    manifest = load_manifest(run_dir)
    print(f"Run: {run_dir.name}")
    print(f"Mode: {mode_for_run(run_dir, manifest)}")
    print("Stages:")
    for name, done in stage_state(run_dir).items():
        print(f"  {name}: {'complete' if done else 'missing'}")
    print(f"Report status: {report_status(run_dir)}")
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
        subprocess.run([runtime, "info"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as error:
        raise ValueError(f"{runtime} daemon/API is unavailable: {error.stderr.strip()}") from error


def validate_existing_run(args: argparse.Namespace) -> None:
    runs_root = args.runs_root.expanduser().resolve()
    run_dir = (runs_root / args.run_dir).resolve()
    if not run_dir.is_dir():
        raise ExitError(f"Run directory not found: {run_dir}", 3)
    if mode_for_run(run_dir) != "emulator":
        raise ValueError("Container-backed validation currently applies only to emulator runs")
    stages = stage_state(run_dir)
    missing = [name for name in ("emulation", "baseline", "blocksci", "report") if not stages[name]]
    if missing:
        raise ExitError(f"Run is incomplete; missing stages: {', '.join(missing)}", 3)
    status = report_status(run_dir)
    if status != "complete":
        raise ExitError(f"Report validation failed: {status}", 4)
    report = load_manifest_json(run_dir / REPORT_DIR / "unified_report.json")
    run_manifest = report.get("run_manifest")
    images = run_manifest.get("images", {}) if isinstance(run_manifest, dict) else {}
    image = args.blocksci_image or (images.get("blocksci") if isinstance(images, dict) else None)
    image = image or "ghcr.io/ondrejman/blocksci-complete:latest"
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
        run_dir
        if args.resume
        else next(parent for parent in (runs_root, *runs_root.parents) if parent.exists())
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
    print(f"Report: {run_dir / REPORT_DIR / 'unified_report.md'}")


def validate_run_id(run_id: str) -> None:
    if Path(run_id).name != run_id or run_id in {"", ".", ".."}:
        raise ValueError("--run-id must be a single directory name")


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
    run_id = shlex.quote(args.run_id)
    network = shlex.quote(args.network)
    coinjoin_type = shlex.quote(args.coinjoin_type)
    return (
        f"mkdir -p /runs/{run_id}/blocksci_data && "
        f"if [ ! -f /runs/{run_id}/blocksci_data/config.json ]; then "
        f"blocksci_parser /runs/{run_id}/blocksci_data/config.json generate-config bitcoin "
        f"/runs/{run_id}/blocksci_data/parsed --disk /mnt/data; fi && "
        f"blocksci_parser /runs/{run_id}/blocksci_data/config.json update && "
        f"python3 /mnt/exporters/unified_report.py --config /runs/{run_id}/blocksci_data/config.json "
        f"--runs-root /runs --run-dir {run_id} --mode external --network {network} "
        f"--coinjoin-type {coinjoin_type} --skip-clustering --markdown"
    )


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Researcher-facing run catalog and external BlockSci analysis.")
    cli.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    add_runtime_argument(cli, default="docker", help_text=None)
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
    external = subcommands.add_parser("external")
    external_commands = external.add_subparsers(dest="external_command", required=True)
    analyze = external_commands.add_parser("analyze")
    analyze.add_argument("--bitcoin-datadir", type=Path)
    analyze.add_argument("--baseline", type=Path)
    analyze.add_argument(
        "--false-cjtxs",
        type=Path,
        action="append",
        help=(
            "Confirmed false-positive JSON file; repeat for fragments. "
            "By default, false_cjtxs.json* files next to --baseline are imported automatically."
        ),
    )
    analyze.add_argument(
        "--network",
        choices=("bitcoin",),
        default="bitcoin",
        help="Blockchain network interpreted by BlockSci (default: bitcoin).",
    )
    add_coinjoin_type_argument(analyze)
    analyze.add_argument("--run-id", required=True)
    analyze.add_argument("--resume", action="store_true")
    analyze.add_argument(
        "--blocksci-image",
        default="ghcr.io/ondrejman/blocksci-complete:latest",
        help="BlockSci container image used for parsing and reporting.",
    )
    analyze.add_argument(
        "--min-free-gb",
        type=int,
        default=20,
        help="Minimum free space required for the persistent BlockSci index (default: 20 GiB).",
    )
    analyze.add_argument("--dry-run", action="store_true")
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
        elif args.command == "scenarios":
            if args.scenarios_command == "list":
                print_scenarios(args.engine)
            else:
                show_or_validate_scenario(args.scenario, args.engine, args.scenarios_command == "show")
        else:
            if not args.resume and (args.bitcoin_datadir is None or args.baseline is None):
                raise ValueError("--bitcoin-datadir and --baseline are required unless --resume is used")
            if args.dry_run:
                dry_run_external(args)
            else:
                external_analyze(args)
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
