"""Input adapters for the single typed configuration schema."""

from __future__ import annotations

import argparse
from dataclasses import MISSING, Field
from functools import lru_cache
from pathlib import Path
from types import NoneType, UnionType
from typing import Literal, Union, get_args, get_origin, get_type_hints

import yaml

from .configuration import (
    ACTION_ALIASES,
    ConfigurationError,
    PipelineAction,
    PipelineConfiguration,
    cli_flag,
    option_routes,
    option_specs,
    set_mapping_path,
)
from .console import EXIT_CODES
from .images import IMAGE_ENVIRONMENT


@lru_cache(maxsize=None)
def field_type(path: str):
    model = PipelineConfiguration
    for part in path.split("."):
        model = get_type_hints(model)[part]
    if get_origin(model) in (Union, UnionType):
        model = next(item for item in get_args(model) if item is not NoneType)
    return model


CONFIGURATION_FLAGS = ("--from-configuration", "--fromConfiguration")
# Host commands that need no experiment; the last five parse their own arguments.
UTILITIES = ("version", "doctor", "pull", "watch", "download-report", "clean-s3", "runs", "scenarios")
DELEGATING_UTILITIES = frozenset({"watch", "download-report", "clean-s3", "runs", "scenarios"})
UTILITY_SUMMARIES = {
    "version": "print the CLI version and the default component images",
    "doctor": "check the container runtime, the runs root and the default images",
    "pull": "pull every component image with the container runtime",
    "watch": "stream Kubernetes and PBS logs of one run",
    "download-report": "download the finished unified report of an S3 run",
    "clean-s3": "delete S3 artifacts of one run or a whole run root",
    "runs": "list, inspect or validate local run directories",
    "scenarios": "list, show or validate the packaged scenarios",
}
ACTION_SUMMARIES = {
    "full-run": "emulate a new run, then analyze it and export its report",
    "emulate": "emulate a new run only",
    "analyze": "BlockSci analysis of --run-dir",
    "export": "unified report of --run-dir",
    "coinjoin-analysis": "baseline coinjoin-analysis of --run-dir or --all-runs (alias: coinjoin)",
    "mappings": "Wasabi mappings and Sake of --run-dir on PBS",
    "pbs-from-s3": "submit PBS analysis jobs for a run already in S3",
    "mainnet-sync": "synchronize Bitcoin Core, Dumplings and the BlockSci index on PBS",
    "initialize": "pull the emulation and analysis images through Compose",
    "clean": "remove the local pipeline containers and volumes (needs --yes)",
    "external analyze": "analyze an external Bitcoin datadir against a baseline",
}

# Argument groups by the first segment of the model path; top-level fields are "".
SECTIONS = {
    "": ("run options", None),
    "kubernetes": ("Kubernetes driver", "Used with --driver kubernetes."),
    "artifacts": ("artifact backend", "S3 options are required by --artifact-backend s3 and pbs-from-s3."),
    "images": (
        "component images",
        "Each image is the explicit option, else its environment variable, else the --local-build tag, "
        "else the release tag --version.",
    ),
    "stages": ("PBS stages", "Run an analyzer as a PBS job instead of a local container."),
    "blocksci": ("BlockSci", "Reusable tasks are submitted with pbs-from-s3 --blocksciPbs."),
    "joinmarket": ("JoinMarket detector", None),
    "mappings": ("Wasabi mappings and Sake", "Used with --mappingsPbs (Wasabi runs only)."),
    "coinjoin_analysis": (
        "coinjoin-analysis programs",
        "Run one upstream program instead of the default baseline; see docs/coinjoin-analysis-operations.md.",
    ),
    "pbs": (
        "PBS resources and images",
        "A stage-specific --pbs-<stage>-* value overrides the shared --pbs-* fallback, "
        "which overrides the stage's default.",
    ),
    "mainnet": ("mainnet-sync", "Only for action mainnet-sync; see docs/mainnet-synchronization.md."),
    "external": ("external analyze", "Only for `cjp external analyze`."),
}

# Help text by model path. The YAML key and the default are appended automatically.
HELP = {
    "runtime": "container runtime for local stages",
    "runs_root": "directory holding the run directories (default: $EMULATION_LOGS_DIR, else ./coinjoin-runs)",
    "engine": "CoinJoin implementation to emulate and analyze",
    "coinjoin_type": "protocol the analyzers look for (default: joinmarket for --engine joinmarket)",
    "driver": "where the emulation runs",
    "scenario": "scenario JSON file, or the file name of a packaged scenario",
    "run_dir": "existing run, as a name inside the runs root or a path to it",
    "run_id": "run ID; emulate and full-run generate <timestamp>_<scenario> when omitted",
    "run_timezone": "IANA timezone of generated run IDs",
    "min_input_count": (
        "Wasabi minimum input count for BlockSci detection and clustering "
        "(default: BlockSci's height-aware threshold; small regtest rounds need an explicit value)"
    ),
    "dry_run": "validate and print the plan without starting containers, jobs or uploads",
    "parallel": "run the local analysis stages of a full-run in parallel",
    "all_runs": "run coinjoin-analysis over every run in the runs root",
    "yes": "confirm the destructive clean action",
    "analysis_action": "collect_docker collects the emulator logs first; analyze_only needs an existing baseline",
    "dumplings_uri": "S3 prefix holding the Dumplings Scanner/ copy and its manifest.json, used as the mainnet baseline",
    "emulation_timeout": "seconds to wait for the emulation before giving up",
    "stage_exporters": "always upload the exporters into the S3 run prefix, even when no PBS stage needs them",
    "kubernetes.namespace": "namespace of the run's Kubernetes resources",
    "kubernetes.reuse_namespace": "use an existing namespace instead of creating and deleting one",
    "kubernetes.kubeconfig": "kubeconfig file (default: ~/.kube/config)",
    "kubernetes.image_prefix": "registry prefix the emulator uses for its infrastructure images",
    "kubernetes.btc_datadir": "shared Bitcoin datadir of the Kubernetes emulation (shared-storage backend only)",
    "kubernetes.copy_to_host": "copy the emulation's Bitcoin data to the host (shared-storage backend only)",
    "kubernetes.infrastructure_local_build": "let the emulator build its infrastructure images locally",
    "artifacts.backend": "where run artifacts are kept between stages",
    "artifacts.uri": "S3 run root, for example s3://bucket/runs",
    "artifacts.endpoint_url": "S3-compatible HTTP(S) endpoint URL",
    "artifacts.secret_name": "Kubernetes Secret with the S3 credentials of the emulation Job",
    "artifacts.credentials_file": "absolute path of the s5cmd credentials file on the frontend",
    "artifacts.profile": "profile name inside the credentials file",
    "images.version": "release tag of every component image; `cjp version` prints the CLI version",
    "images.local_build": "use the images ./run-all.sh local builds (*:local)",
    "images.uploader": "S3 uploader image (also $COINJOIN_UPLOADER_IMAGE; default: container/uploader.image)",
    "images.unified_report": (
        "unified-report job image (also $COINJOIN_UNIFIED_REPORT_IMAGE; default: container/unified-report.image)"
    ),
    "stages.analysis": "run coinjoin-analysis as a PBS job",
    "stages.blocksci": "run BlockSci as a PBS job",
    "stages.mappings": "run the Wasabi mappings enumerator and Sake as PBS jobs",
    "blocksci.workflow": "combined parses and detects in one job; reusable keeps the index; cached reuses one",
    "blocksci.task": "what the BlockSci job does; detect is the regular analysis",
    "blocksci.script": "Python script of --blocksci-task script",
    "blocksci.cache_source_run_id": "run whose BlockSci index cache this task reads",
    "blocksci.notebook_port": "preferred Jupyter port of --blocksci-task notebook (1024-65535)",
    "blocksci.notebooks_dir": "notebook directory of --blocksci-task notebook",
    "blocksci.external_bitcoin_datadir": "Bitcoin datadir to parse (reusable parse or cached update)",
    "blocksci.bitcoin_blocks_uri": "S3 prefix of a bitcoin-block-archive to parse (reusable parse or cached update)",
    "blocksci.external_blocksci_dir": "existing BlockSci index directory for a reusable parse",
    "blocksci.external_baseline_uri": "S3 URI of the baseline compared by --blocksci-task external",
    "blocksci.external_false_cjtxs_uri": "S3 URI of curated Dumplings false positives (false_cjtxs.json)",
    "blocksci.network": "chain of an external Bitcoin source",
    "blocksci.max_block": "last height to parse from an external Bitcoin source",
    "blocksci.expected_block_hash": "expected block hash at --blocksci-max-block (required for mainnet archives)",
    "blocksci.pin_height": "script or external task: the last height the analysis may see",
    "blocksci.scan_start_height": "script or external task: first scanned height (inclusive)",
    "blocksci.scan_end_height": "script or external task: end of the scanned range (exclusive)",
    "blocksci.index_dir": (
        "persistent BlockSci index under /storage/ or /scratch/multijob-<location>/ instead of the S3 cache"
    ),
    "blocksci.index_backup_uri": "S3 prefix of the persistent index's backup (one archive and its manifest)",
    "blocksci.index_state_uri": (
        "Fixed S3 prefix of the persistent index's parse, update and backup jobs in place of the run prefix; "
        "each job replaces its markers and manifest there and keeps its logs under logs/<run-id>/"
    ),
    "pbs.image": "Singularity image of every PBS stage without its own --pbs-*-image",
    "pbs.bitcoin_datadir": "Bitcoin datadir the PBS BlockSci stage reads (also $PBS_BITCOIN_DATADIR)",
    "coinjoin_analysis.actions": "program operation (repeatable)",
    "coinjoin_analysis.target_paths": "program target path (repeatable)",
    "coinjoin_analysis.input_uri": "S3 input of the program (pbs-from-s3)",
    "coinjoin_analysis.input_dir": "local input directory of the program (coinjoin-analysis)",
    "coinjoin_analysis.research.input_indices": "input index (repeatable)",
    "coinjoin_analysis.research.output_indices": "output index (repeatable)",
    "external.bitcoin_datadir": "Bitcoin datadir to analyze",
    "external.baseline": "baseline to compare the detections against",
    "external.false_cjtxs": "false_cjtxs.json applied to the baseline (repeatable)",
    "external.min_free_gb": "free space the run directory needs, in GiB (default: 20)",
    "external.resume": "resume the external analysis of --run-id",
    **{
        f"images.{component}": f"{component.replace('_', '-')} image (also ${variable})"
        for component, variable in IMAGE_ENVIRONMENT.items()
    },
    **{
        f"pbs.{component}_image": f"Singularity image of the {component.replace('_', '-')} PBS stage"
        for component in ("blocksci", "coinjoin_analysis", "mappings_enumerator", "sake")
    },
    **{
        f"pbs.{name}": f"shared fallback {name.replace('_', '-')} of every PBS stage"
        for name in ("ncpus", "mem", "scratch", "scratch_type", "walltime")
    },
}


def option_flags(path: str, destination: str) -> list[str]:
    flags = [cli_flag(destination)]
    if path == "blocksci.script":
        flags.append("--blocksciScript")
    return flags


def option_help(path: str, item: Field) -> str:
    """Help text of one option: its description, YAML key and effective default."""
    parts = path.split(".")
    if parts[0] == "pbs" and len(parts) == 3:
        stage, name = parts[1], parts[2]
        text = f"{stage.replace('_', '-')} PBS job {name.replace('_', '-')}"
        if name == "multijob":
            text += " scratch location"
        else:
            text += f"; falls back to --pbs-{name.replace('_', '-')}"
    else:
        text = HELP.get(path, "")
    details = [f"YAML: {path}"]
    default = item.default
    if default is not MISSING and default not in (None, False, ()):
        details.append(f"default: {default}")
    suffix = "[" + "; ".join(details) + "]"
    return f"{text} {suffix}" if text else suffix


def _metavar(path: str) -> str:
    if path.endswith("_image") or (path.startswith("images.") and path != "images.version"):
        return "IMAGE"
    return path.rsplit(".", 1)[-1].upper()


def action_index(argv: list[str]) -> int | None:
    """Return the position of the action word, skipping options and their values."""
    takes_value = set(CONFIGURATION_FLAGS)
    for path, destination, _ in option_specs():
        if field_type(path) is not bool:
            takes_value.update(option_flags(path, destination))
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return None
        if not token.startswith("-"):
            return index
        index += 2 if token in takes_value else 1
    return None


def action_word(argv: list[str]) -> str | None:
    index = action_index(argv)
    return None if index is None else argv[index]


def _epilog() -> str:
    width = max(map(len, (*ACTION_SUMMARIES, *UTILITY_SUMMARIES)))
    lines = ["actions:"]
    lines += [f"  {name:<{width}}  {summary}" for name, summary in ACTION_SUMMARIES.items()]
    lines += ["", "utilities (watch, download-report, clean-s3, runs and scenarios accept their own --help):"]
    lines += [f"  {name:<{width}}  {summary}" for name, summary in UTILITY_SUMMARIES.items()]
    lines += [
        "",
        "examples:",
        "  cjp run examples/metacentrum-s3.yaml --dry-run",
        "  cjp full-run --engine joinmarket --dry-run",
        "  cjp analyze --run-dir <run-id>",
        "  cjp watch --run-id <run-id> --pbs-only",
        "",
        "Every option is also a YAML key (shown as YAML: ...); options after `run CONFIG.yaml` override it.",
        "Use --dry-run to validate and render an execution without starting work.",
        "",
        "exit codes:",
    ]
    lines += [f"  {code:<3}  {meaning}" for code, meaning in EXIT_CODES]
    return "\n".join(lines)


def build_parser(paths: set[str] | None = None, *, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cjp",
        usage=(
            "cjp ACTION [OPTIONS]\n"
            "       cjp run CONFIG.yaml [OPTIONS]\n"
            "       cjp UTILITY [UTILITY OPTIONS]"
        ),
        description="Run a CoinJoin experiment from YAML or explicit options.",
        add_help=add_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_epilog(),
    )
    parser.add_argument("action", nargs="?", default=argparse.SUPPRESS, help="execution action (listed below)")
    parser.add_argument(
        *CONFIGURATION_FLAGS,
        type=Path,
        metavar="CONFIG.yaml",
        help="load an experiment from YAML (same as `cjp run CONFIG.yaml`)",
    )
    # Created in SECTIONS order, which is the order --help lists them in.
    groups = {section: parser.add_argument_group(title, text) for section, (title, text) in SECTIONS.items()}
    for path, destination, item in option_specs():
        if paths is not None and path not in paths:
            continue
        section = path.split(".", 1)[0] if "." in path else ""
        annotation = field_type(path)
        options: dict[str, object] = {
            "dest": destination,
            "default": argparse.SUPPRESS,
            "help": option_help(path, item),
        }
        if annotation is bool:
            options["action"] = "store_true"
        elif get_origin(annotation) is tuple:
            options.update(action="append", type=get_args(annotation)[0])
        elif get_origin(annotation) is Literal:
            options.update(choices=get_args(annotation))
        else:
            options.update(type=annotation, metavar=_metavar(path))
        groups[section].add_argument(*option_flags(path, destination), **options)  # type: ignore[arg-type]
    return parser


def _canonical_action(action: str) -> str:
    action = ACTION_ALIASES.get(action, action)
    if action not in get_args(PipelineAction):
        raise ConfigurationError(
            f"unknown action {action!r}; choose one of: {', '.join(get_args(PipelineAction))}, "
            f"or a utility: {', '.join(UTILITIES)}"
        )
    return action


def load_configuration(argv: list[str]) -> PipelineConfiguration:
    """Merge YAML and explicit options as values, without rendering/parsing argv."""
    index = action_index(argv)
    if index is not None:
        head, action, tail = argv[:index], argv[index], argv[index + 1 :]
        if action == "container" and tail[:1] and tail[0] in {"docker", "podman"}:
            argv = [*head, "--runtime", tail[0], *tail[1:]]
        elif action == "run":
            if tail[:1] and tail[0] in {"-h", "--help"}:
                argv = [*head, *tail]
            elif not tail or tail[0].startswith("-"):
                raise ConfigurationError("run requires a YAML configuration path: cjp run CONFIG.yaml")
            else:
                argv = [*head, "--from-configuration", tail[0], *tail[1:]]
        elif action == "external" and tail[:1] == ["analyze"]:
            argv = [*head, "external analyze", *tail[1:]]
    parsed = vars(build_parser().parse_args(argv))
    path = parsed.pop("from_configuration", None)
    data = {}
    if path is not None:
        if "action" in parsed:
            raise ConfigurationError("put the action in YAML when using --from-configuration")
        try:
            data = yaml.safe_load(path.expanduser().read_text(encoding="utf-8"))
            if data is None:
                data = {}
        except (OSError, yaml.YAMLError) as error:
            raise ConfigurationError(f"cannot read configuration {path}: {error}") from error
        if not isinstance(data, dict):
            raise ConfigurationError("configuration must be a mapping")
    routes = option_routes()
    for key, value in parsed.items():
        if key == "action":
            value = _canonical_action(value)
        set_mapping_path(data, routes[key], value)
    return PipelineConfiguration.from_mapping(data)
