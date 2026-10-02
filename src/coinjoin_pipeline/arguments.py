"""Input adapters for the single typed configuration schema."""

from __future__ import annotations

import argparse
from functools import lru_cache
from pathlib import Path
from types import NoneType, UnionType
from typing import Literal, Union, get_args, get_origin, get_type_hints

import yaml

from .configuration import (
    ConfigurationError,
    PipelineConfiguration,
    option_specs,
    set_mapping_path,
)
from .option_rules import ACTION_ALIASES


@lru_cache(maxsize=None)
def field_type(path: str):
    model = PipelineConfiguration
    for part in path.split("."):
        model = get_type_hints(model)[part]
    if get_origin(model) in (Union, UnionType):
        model = next(item for item in get_args(model) if item is not NoneType)
    return model


CONFIGURATION_FLAGS = ("--from-configuration", "--fromConfiguration")


def option_flags(path: str, destination: str) -> list[str]:
    flags = ["--" + destination.replace("_", "-")]
    if path == "blocksci.script":
        flags.append("--blocksciScript")
    return flags


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


def build_parser(paths: set[str] | None = None, *, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a CoinJoin experiment from YAML.", add_help=add_help)
    parser.add_argument("action", nargs="?", default=argparse.SUPPRESS)
    parser.add_argument(*CONFIGURATION_FLAGS, type=Path)
    for path, destination, _ in option_specs():
        if paths is not None and path not in paths:
            continue
        annotation = field_type(path)
        flags = option_flags(path, destination)
        options = {"dest": destination, "default": argparse.SUPPRESS}
        if annotation is bool:
            options["action"] = "store_true"
        elif get_origin(annotation) is tuple:
            options.update(action="append", type=str)
        elif get_origin(annotation) is Literal:
            options.update(choices=get_args(annotation))
        else:
            options["type"] = annotation
        parser.add_argument(*flags, **options)
    return parser


def load_configuration(argv: list[str]) -> PipelineConfiguration:
    """Merge YAML and explicit options as values, without rendering/parsing argv."""
    index = action_index(argv)
    if index is not None:
        head, action, tail = argv[:index], argv[index], argv[index + 1 :]
        if action == "container" and tail[:1] and tail[0] in {"docker", "podman"}:
            argv = [*head, "--runtime", tail[0], *tail[1:]]
        elif action == "run":
            if not tail or tail[0].startswith("-"):
                raise ConfigurationError("run requires a YAML configuration path")
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
    routes = {destination: path for path, destination, _ in option_specs()}
    routes["action"] = "action"
    for key, value in parsed.items():
        if key == "action":
            value = ACTION_ALIASES.get(value, value)
        set_mapping_path(data, routes[key], value)
    return PipelineConfiguration.from_mapping(data)
