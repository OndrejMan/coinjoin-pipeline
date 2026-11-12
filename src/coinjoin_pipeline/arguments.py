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


CONFIGURATION_FLAGS = ("--from-configuration",)
# Host commands that need no experiment.
UTILITIES = (
    "version",
    "doctor",
    "pull",
)
UTILITY_SUMMARIES = {
    "version": "print the CLI version and the default component images",
    "doctor": "check the container runtime, the runs root and the default images",
    "pull": "pull every component image with the container runtime",
}
ACTION_SUMMARIES = {
    "full-run": "emulate a new run, then analyze it and export its report",
    "emulate": "emulate a new run only",
    "analyze": "BlockSci analysis of --run-dir",
    "export": "unified report of --run-dir",
    "coinjoin-analysis": "baseline coinjoin-analysis of --run-dir or --all-runs",
    "initialize": "pull the emulation and analysis images through Compose",
    "clean": "remove the local pipeline containers and volumes (needs --yes)",
}

# Argument groups by the first segment of the model path; top-level fields are "".
SECTIONS = {
    "": ("run options", None),
    "kubernetes": ("Kubernetes driver", "Used with --driver kubernetes."),
    "artifacts": ("artifact backend", "S3 options are required by --artifact-backend s3."),
    "images": (
        "component images",
        "Each image is the explicit option, else its environment variable, else the --local-build tag, "
        "else the release tag --version.",
    ),
    "stages": ("PBS stages", "Run an analyzer as a PBS job instead of a local container."),
    "blocksci": ("BlockSci", None),
    "joinmarket": ("JoinMarket detector", None),
    "pbs": (
        "PBS resources and images",
        "A stage-specific --pbs-<stage>-* value overrides the shared --pbs-* fallback, "
        "which overrides the stage's default.",
    ),
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
    "emulation_timeout": "seconds to wait for the emulation before giving up",
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
    "images.registry": (
        "registry and owner of the project's images, e.g. ghcr.io/you (also $COINJOIN_IMAGE_REGISTRY; "
        "default: ghcr.io/ondrejman)"
    ),
    "images.local_build": "use locally built images (*:local)",
    "images.uploader": "S3 uploader image (default: container/uploader.image)",
    "images.unified_report": ("unified-report job image (default: container/unified-report.image)"),
    "blocksci.script": "Python script run over the parsed BlockSci index",
    "pbs.image": "Singularity image of every PBS stage without its own --pbs-*-image",
    **{
        f"images.{component}": f"{component.replace('_', '-')} image (also ${variable})"
        for component, variable in IMAGE_ENVIRONMENT.items()
    },
    **{
        f"pbs.{component}_image": f"Singularity image of the {component.replace('_', '-')} PBS stage"
        for component in ("blocksci",)
    },
    **{
        f"pbs.{name}": f"shared fallback {name.replace('_', '-')} of every PBS stage"
        for name in ("ncpus", "mem", "scratch", "scratch_type", "walltime")
    },
}


def option_help(path: str, item: Field) -> str:
    """Help text of one option: its description, YAML key and effective default."""
    parts = path.split(".")
    if parts[0] == "pbs" and len(parts) == 3:
        stage, name = parts[1], parts[2]
        text = f"{stage.replace('_', '-')} PBS job {name.replace('_', '-')}"
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
            takes_value.add(cli_flag(destination))
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
    lines += ["", "utilities:"]
    lines += [f"  {name:<{width}}  {summary}" for name, summary in UTILITY_SUMMARIES.items()]
    lines += [
        "",
        "examples:",
        "  cjp full-run --engine joinmarket --dry-run",
        "  cjp analyze --run-dir <run-id>",
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
        usage=("cjp ACTION [OPTIONS]\n       cjp run CONFIG.yaml [OPTIONS]\n       cjp UTILITY [UTILITY OPTIONS]"),
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
        groups[section].add_argument(cli_flag(destination), **options)  # type: ignore[arg-type]
    return parser


def _canonical_action(action: str) -> str:
    if action not in get_args(PipelineAction):
        raise ConfigurationError(
            f"unknown action {action!r}; choose one of: {', '.join(get_args(PipelineAction))}, "
            f"or a utility: {', '.join(UTILITIES)}"
        )
    return action


def load_configuration(argv: list[str]) -> PipelineConfiguration:
    """Decode the YAML file, when given, and the explicit options into one configuration."""
    index = action_index(argv)
    if index is not None:
        head, action, tail = argv[:index], argv[index], argv[index + 1 :]
        if action == "run":
            if tail[:1] and tail[0] in {"-h", "--help"}:
                argv = [*head, *tail]
            elif not tail or tail[0].startswith("-"):
                raise ConfigurationError("run requires a YAML configuration path: cjp run CONFIG.yaml")
            else:
                argv = [*head, "--from-configuration", tail[0], *tail[1:]]
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
