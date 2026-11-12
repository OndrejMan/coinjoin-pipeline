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
ACTION_SUMMARIES = {
    "full-run": "emulate a new run, then analyze it and export its report",
    "emulate": "emulate a new run only",
    "analyze": "BlockSci analysis of --run-dir",
    "export": "unified report of --run-dir",
    "coinjoin-analysis": "baseline coinjoin-analysis of --run-dir",
}

# Argument groups by the first segment of the model path; top-level fields are "".
SECTIONS = {
    "": ("run options", None),
    "images": (
        "component images",
        "Each image is the explicit option, else its environment variable, else the release tag --version.",
    ),
}

# Help text by model path. The YAML key and the default are appended automatically.
HELP = {
    "runtime": "container runtime for local stages",
    "runs_root": "directory holding the run directories (default: $EMULATION_LOGS_DIR, else ./coinjoin-runs)",
    "engine": "CoinJoin implementation to emulate and analyze",
    "coinjoin_type": "protocol the analyzers look for (default: joinmarket for --engine joinmarket)",
    "scenario": "scenario JSON file, or the file name of a packaged scenario",
    "run_dir": "existing run, as a name inside the runs root or a path to it",
    "run_id": "run ID; emulate and full-run generate <timestamp>_<scenario> when omitted",
    "run_timezone": "IANA timezone of generated run IDs",
    "min_input_count": (
        "Wasabi minimum input count for BlockSci detection "
        "(default: BlockSci's height-aware threshold; small regtest rounds need an explicit value)"
    ),
    "images.version": "release tag of every component image",
    "images.unified_report": ("unified-report job image (default: container/unified-report.image)"),
    **{
        f"images.{component}": f"{component.replace('_', '-')} image (also ${variable})"
        for component, variable in IMAGE_ENVIRONMENT.items()
    },
}


def option_help(path: str, item: Field) -> str:
    """Help text of one option: its description, YAML key and effective default."""
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


def _epilog() -> str:
    width = max(map(len, ACTION_SUMMARIES))
    lines = ["actions:"]
    lines += [f"  {name:<{width}}  {summary}" for name, summary in ACTION_SUMMARIES.items()]
    lines += [
        "",
        "examples:",
        "  cjp full-run --engine joinmarket",
        "  cjp analyze --run-dir <run-id>",
        "",
        "Every option is also a YAML key (shown as YAML: ...); options after `run CONFIG.yaml` override it.",
        "",
        "exit codes:",
    ]
    lines += [f"  {code:<3}  {meaning}" for code, meaning in EXIT_CODES]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cjp",
        usage=("cjp ACTION [OPTIONS]\n       cjp run CONFIG.yaml [OPTIONS]"),
        description="Run a CoinJoin experiment from YAML or explicit options.",
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
        raise ConfigurationError(f"unknown action {action!r}; choose one of: {', '.join(get_args(PipelineAction))}")
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
