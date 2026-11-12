"""Adapters for the existing public coinjoin-analysis commands; no analyzer extensions."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
from typing import TYPE_CHECKING
from uuid import uuid4

from exporters.artifact_paths import COINJOIN_ANALYSIS_DIR, COINJOIN_ANALYSIS_TOOLS_DIR, EMULATOR_DIR

from coinjoin_pipeline.storage.s3 import validate_artifact_uri
from coinjoin_pipeline.images import image_names

if TYPE_CHECKING:
    from coinjoin_pipeline.configuration import CoinjoinAnalysisConfiguration, PipelineConfiguration

PROGRAM_ACTIONS = {
    "parse_cj_logs": ("collect_local", "collect_docker", "analyze_only"),
    "parse_dumplings": (
        "process_dumplings",
        "detect_false_positives",
        "detect_coordinators",
        "split_coordinators",
        "plot_coinjoins",
    ),
    "ww2_analyze_client": ("process_as25as38", "process_emulations", "compute_num_predict_matrix"),
    "scan_results_plot": (),
    "file_check": (),
    "visualize_coordinators": (),
    "perf_processing": (),
}
POSITIONAL_PROGRAMS = frozenset({"scan_results_plot", "file_check", "visualize_coordinators", "perf_processing"})
MAINNET_PROGRAMS = frozenset({"parse_dumplings", "visualize_coordinators"})
EMULATION_PROGRAMS = frozenset({"parse_cj_logs"})


def relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\x00" not in value


def needs_external_input(options: CoinjoinAnalysisConfiguration) -> bool:
    return (
        options.program in {"parse_dumplings", *POSITIONAL_PROGRAMS}
        or options.actions == ("collect_local",)
        or (options.program == "ww2_analyze_client" and options.actions != ("process_emulations",))
    )


def option_errors(config: PipelineConfiguration) -> list[str]:
    options = config.coinjoin_analysis
    if options.program is None:
        return (
            ["coinjoin_analysis options require --analysis-program"]
            if any(path.startswith("coinjoin_analysis.") for path in config.provided)
            else []
        )
    return [
        *_data_mode_errors(options),
        *_placement_errors(config),
        *_operation_errors(options),
        *_input_errors(config),
    ]


def _data_mode_errors(options: CoinjoinAnalysisConfiguration) -> list[str]:
    """Mainnet operations read public-chain data; emulation operations need wallet labels."""
    errors = []
    if options.program in MAINNET_PROGRAMS and options.data_mode != "mainnet":
        errors.append(f"{options.program} is a mainnet operation; set --analysis-data-mode mainnet")
    if options.program in EMULATION_PROGRAMS and options.data_mode != "emulation":
        errors.append(f"{options.program} requires emulation data with wallet labels")
    if options.program == "ww2_analyze_client":
        if options.data_mode == "mainnet" and "process_emulations" in options.actions:
            errors.append("process_emulations belongs to emulation data")
        if options.data_mode == "emulation" and any(action != "process_emulations" for action in options.actions):
            errors.append("as25/as38 client experiments and their prediction matrix belong to mainnet")
    return errors


def _placement_errors(config: PipelineConfiguration) -> list[str]:
    """Analyzer operations run alone, locally through coinjoin-analysis or on PBS through pbs-from-s3."""
    errors = []
    if config.action not in {"coinjoin-analysis", "pbs-from-s3"}:
        errors.append("--analysis-program requires coinjoin-analysis or pbs-from-s3")
    if config.action == "pbs-from-s3" and not config.stages.analysis:
        errors.append("--analysis-program on PBS requires --analysis-pbs")
    if config.stages.blocksci or config.stages.mappings or config.all_runs or config.dumplings_uri:
        errors.append("coinjoin-analysis operations run alone; disable BlockSci/mappings and select one run")
    if config.action == "coinjoin-analysis" and config.stages.analysis:
        errors.append("analysis operations on PBS use pbs-from-s3; local operations use Docker/Podman")
    return errors


def _operation_errors(options: CoinjoinAnalysisConfiguration) -> list[str]:
    """The selected operations and the switches the program accepts."""
    errors = []
    program = options.program or ""
    allowed = PROGRAM_ACTIONS[program]
    if allowed and not options.actions:
        errors.append("choose at least one --analysis-operation")
    if any(action not in allowed for action in options.actions):
        errors.append(f"invalid operation for {program}; choose from {', '.join(allowed)}")
    if program == "parse_cj_logs" and len(options.actions) != 1:
        errors.append("parse_cj_logs accepts exactly one operation")
    if program == "scan_results_plot":
        filename = options.filename
        if not filename or not relative_path(filename) or "/" in filename or filename.endswith(".json"):
            errors.append("scan_results_plot requires --analysis-filename (a filename without .json)")
    elif options.filename is not None:
        errors.append("--analysis-filename is only supported by scan_results_plot")
    if program in POSITIONAL_PROGRAMS and (options.env_vars or options.load_config):
        errors.append(f"{program} has no --env_vars or --load-config option")
    if options.load_config and program not in {"parse_cj_logs", "parse_dumplings"}:
        errors.append("--analysis-load-config is supported by parse_cj_logs and parse_dumplings")
    return errors


def _input_errors(config: PipelineConfiguration) -> list[str]:
    """Target paths stay inside the input dataset, which comes from S3 on PBS and a directory locally."""
    options = config.coinjoin_analysis
    errors = []
    if options.program != "parse_cj_logs" and len(options.target_paths) > 1:
        errors.append(f"{options.program} accepts only one target path")
    if options.program == "perf_processing" and not options.target_paths:
        errors.append("perf_processing requires --analysis-target-path pointing to a CSV file")
    if any(
        not relative_path(path)
        for path in (*options.target_paths, *([options.load_config] if options.load_config else []))
    ):
        errors.append("analysis target/config paths must be relative to the input and must not contain '..'")
    if needs_external_input(options) and not (options.input_uri or options.input_dir):
        errors.append("this operation needs its own input dataset (--analysis-input-uri or --analysis-input-dir)")
    if options.input_uri:
        try:
            validate_artifact_uri(options.input_uri)
        except ValueError as error:
            errors.append(f"invalid analysis input URI: {error}")
        if config.action != "pbs-from-s3":
            errors.append("--analysis-input-uri requires pbs-from-s3; use --analysis-input-dir locally")
    if options.input_dir and config.action != "coinjoin-analysis":
        errors.append("--analysis-input-dir is local; use --analysis-input-uri on PBS")
    return errors


def command_argv(options: CoinjoinAnalysisConfiguration, run_id: str) -> list[str]:
    """Pass public CLI flags as values, retaining multiple actions/targets where supported."""
    if options.program not in PROGRAM_ACTIONS:
        raise ValueError("select a coinjoin-analysis program")
    default = (
        run_id if options.program == "ww2_analyze_client" and not (options.input_uri or options.input_dir) else "."
    )
    targets = [str(PurePosixPath("/work/input") / path) for path in options.target_paths or (default,)]
    module = "helpers.perf_processing" if options.program == "perf_processing" else f"cj_process.{options.program}"
    argv = ["python", "-m", module]
    if options.program in POSITIONAL_PROGRAMS:
        return [*argv, targets[0], *([options.filename or ""] if options.program == "scan_results_plot" else [])]
    if options.program != "parse_cj_logs":
        argv.extend(["--cjtype", options.cjtype])
    for action in options.actions:
        argv.extend(["--action", action])
    for target in targets:
        argv.extend(["--target-path", target])
    if options.load_config:
        argv.extend(["--load-config", str(PurePosixPath("/work/input") / options.load_config)])
    if options.env_vars:
        argv.extend(["--env_vars", options.env_vars])
    return argv


def command(options: CoinjoinAnalysisConfiguration, run_id: str) -> str:
    return shlex.join(command_argv(options, run_id))


def run_local(config: PipelineConfiguration, run_dir: Path) -> Path:
    """Run the shipped image command on a writable copy, retaining the canonical baseline."""
    options = config.coinjoin_analysis
    output = run_dir / COINJOIN_ANALYSIS_TOOLS_DIR / str(options.program) / uuid4().hex
    image = (
        config.images.coinjoin_analysis
        or f"{image_names(config.images.registry)['coinjoin_analysis']}:{config.images.version or 'latest'}"
    )
    analyzer = command_argv(options, run_dir.name)
    argv = [
        config.runtime,
        "run",
        "--rm",
        "--entrypoint",
        analyzer[0],
        "-w",
        "/work",
        "--env",
        "MPLBACKEND=Agg",
        "--env",
        "MPLCONFIGDIR=/tmp/matplotlib",
        "--volume",
        f"{output}:/work:rw",
        image,
        *analyzer[1:],
    ]
    if config.dry_run:
        print(f"[dry-run] Copy analysis inputs into {output}/input")
        print(shlex.join(argv))
        return output
    source = Path(options.input_dir).expanduser().resolve() if options.input_dir else run_dir
    if not source.is_dir():
        raise ValueError(f"Analysis input directory does not exist: {source}")
    if source == output or (source in output.parents and options.input_dir):
        raise ValueError("Analysis input directory must not contain the output directory")
    if options.input_dir:
        shutil.copytree(source, output / "input")
    else:
        experiment = output / "input" / run_dir.name
        baseline, data = run_dir / COINJOIN_ANALYSIS_DIR, run_dir / EMULATOR_DIR / "data"
        if not baseline.is_dir() and not data.is_dir():
            raise ValueError("The run has neither a coinjoin-analysis baseline nor emulator data")
        experiment.mkdir(parents=True)
        if baseline.is_dir():
            shutil.copytree(baseline, experiment, dirs_exist_ok=True)
        if data.is_dir():
            shutil.copytree(data, experiment / "data", dirs_exist_ok=True)
    (output / "invocation.json").write_text(
        json.dumps(
            {"argv": command_argv(options, run_dir.name), "image": image, "input_dir": options.input_dir}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"coinjoin-analysis outputs and log: {output}")
    with (output / "analysis.log").open("w", encoding="utf-8") as log:
        subprocess.run(argv, check=True, stdout=log, stderr=subprocess.STDOUT)
    return output
