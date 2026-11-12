"""Typed configuration shared by YAML, the command line and orchestration.

Each field is declared once. Input adapters decode values into this model;
execution consumes the model directly, without an argv round trip.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, is_dataclass, replace
from functools import lru_cache
from pathlib import Path
from types import NoneType, UnionType
from typing import Literal, Mapping, TypeVar, Union, get_args, get_origin, get_type_hints

T = TypeVar("T")


class ConfigurationError(ValueError):
    """Invalid configuration at the input boundary."""


def required(value: T | None, name: str) -> T:
    """Return a value that validation already requires in the current mode."""
    if value is None:
        raise ConfigurationError(f"{name} is required")
    return value


PipelineAction = Literal[
    "full-run",
    "emulate",
    "analyze",
    "export",
    "coinjoin-analysis",
]
Engine = Literal["wasabi", "joinmarket"]
CoinjoinType = Literal["wasabi2", "joinmarket"]
ContainerRuntime = Literal["docker", "podman"]
AnalysisAction = Literal["collect_docker", "analyze_only"]


def option(default=None, *, name=None, minimum=None, actions=None):
    """One configuration field; ``name`` is its CLI destination (help text lives in arguments.py)."""
    # Called only inside the dataclass bodies below; pylint cannot see through the helper.
    return field(  # pylint: disable=invalid-field-call
        default=default, metadata={"option": name, "minimum": minimum, "actions": actions}
    )


class ConfigurationModel:
    @classmethod
    def from_mapping(cls, value, location=None):
        return decode_model(cls, value, location or cls.__name__)


@dataclass(frozen=True, slots=True)
class ImageConfiguration(ConfigurationModel):
    version: str | None = option(None, name="version")
    registry: str | None = option(None, name="image_registry")
    local_build: bool = option(False, name="local_build")
    emulator: str | None = option(None, name="emulator_image")
    coinjoin_analysis: str | None = option(None, name="coinjoin_analysis_image")
    blocksci: str | None = option(None, name="blocksci_image")
    unified_report: str | None = option(None, name="unified_report_image")


@dataclass(frozen=True, slots=True)
class PipelineConfiguration(ConfigurationModel):
    action: PipelineAction = option("full-run")
    runtime: ContainerRuntime = option("docker", name="runtime")
    runs_root: str | None = option(None, name="runs_root")
    engine: Engine = option("wasabi", name="engine")
    coinjoin_type: CoinjoinType = option("wasabi2", name="coinjoin_type")
    scenario: str | None = option(None, name="scenario")
    run_dir: str | None = option(None, name="run_dir", actions=("analyze", "export", "coinjoin-analysis"))
    run_id: str | None = option(None, name="run_id", actions=("emulate", "full-run"))
    run_timezone: str = option("Europe/Prague", name="run_timezone")
    min_input_count: int | None = option(None, name="min_input_count", minimum=1)
    dry_run: bool = option(False, name="dry_run")
    parallel: bool = option(False, name="parallel", actions=("full-run",))
    all_runs: bool = option(False, name="all_runs", actions=("coinjoin-analysis",))
    analysis_action: AnalysisAction = option("collect_docker", name="analysis_action", actions=("coinjoin-analysis",))
    images: ImageConfiguration = field(default_factory=ImageConfiguration)
    _provided: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)

    @property
    def provided(self) -> frozenset[str]:
        """Model paths the input set explicitly, as opposed to defaults."""
        return self._provided

    @property
    def runs_path(self) -> Path:
        """The runs root, which configuration resolution always sets."""
        return Path(required(self.runs_root, "runs_root"))

    @classmethod
    def from_mapping(cls, value, location=None):
        data = dict(require_mapping(value, "configuration"))
        model = decode_model(cls, data, "configuration")
        provided = frozenset(flatten_mapping(data))
        return replace(model, _provided=provided)

    @classmethod
    def from_flat(cls, values: Mapping[str, object]):
        mapping: dict[str, object] = {}
        routes = option_routes()
        for dest, value in values.items():
            if dest not in routes:
                raise ConfigurationError(f"unsupported option: {dest}")
            set_mapping_path(mapping, routes[dest], value)
        return cls.from_mapping(mapping)

    def validate(self) -> None:
        from .option_rules import cross_option_errors

        errors = cross_option_errors(self)
        if errors:
            raise ConfigurationError("; ".join(errors))


@lru_cache(maxsize=None)
def model_annotations(model_type):
    return get_type_hints(model_type)


def require_mapping(value, location):
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{location} must be a mapping")
    return value


def decode_value(annotation, value, location):
    if is_dataclass(annotation):
        return decode_model(annotation, value, location)
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        if value is None and NoneType in arguments:
            return None
        annotation = next(item for item in arguments if item is not NoneType)
        return decode_value(annotation, value, location)
    if origin is Literal:
        if value not in arguments or isinstance(value, bool):
            raise ConfigurationError(f"{described(location)} must be one of: {', '.join(map(str, arguments))}")
        return value
    if origin is tuple:
        if arguments[0] is not str:
            if not isinstance(value, (list, tuple)):
                raise ConfigurationError(f"{described(location)} must be a list")
            return tuple(decode_value(arguments[0], item, f"{location}[{index}]") for index, item in enumerate(value))
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, (list, tuple)) or not all(isinstance(item, str) for item in values):
            raise ConfigurationError(f"{described(location)} must be a string or a list of strings")
        return tuple(values)
    # Exact type: bool is an int subclass and must not pass as a number.
    valid = type(value) is annotation  # pylint: disable=unidiomatic-typecheck
    if annotation is float:
        valid = type(value) in (int, float) and math.isfinite(value)  # pylint: disable=unidiomatic-typecheck
        if valid:
            return float(value)
    if not valid:
        expected = {
            str: "a string",
            bool: "true or false",
            int: "an integer",
            float: "a finite number",
        }.get(annotation, str(annotation))
        raise ConfigurationError(f"{described(location)} must be {expected}")
    return value


def decode_model(model_type, value, location):
    data = require_mapping(value, location)
    schema = {item.name: item for item in fields(model_type) if not item.name.startswith("_")}
    unknown = set(data) - schema.keys()
    if unknown:
        raise ConfigurationError(f"unsupported {location} key(s): {', '.join(sorted(map(str, unknown)))}")
    annotations = model_annotations(model_type)
    values = {}
    for key, raw in data.items():
        item = schema[key]
        value = decode_value(annotations[key], raw, f"{location}.{key}")
        minimum = item.metadata.get("minimum")
        if value is not None and minimum is not None and value < minimum:
            label = "positive" if minimum == 1 else "non-negative"
            raise ConfigurationError(f"{described(f'{location}.{key}')} must be {label}")
        values[key] = value
    return model_type(**values)


def option_specs(model_type=PipelineConfiguration, prefix=""):
    """Yield (model path, CLI destination, field) from the model's declarations."""
    annotations = model_annotations(model_type)
    for item in fields(model_type):
        if item.name.startswith("_") or item.name == "action":
            continue
        path = f"{prefix}.{item.name}" if prefix else item.name
        annotation = annotations[item.name]
        if is_dataclass(annotation):
            yield from option_specs(annotation, path)
        else:
            destination = item.metadata.get("option")
            yield path, destination, item


def cli_flag(destination: str) -> str:
    """The command-line spelling of one option destination."""
    return "--" + destination.replace("_", "-")


@lru_cache(maxsize=None)
def option_routes() -> dict[str, str]:
    """CLI destination -> model path, for every option and the action."""
    routes = {destination: path for path, destination, _ in option_specs()}
    routes["action"] = "action"
    return routes


@lru_cache(maxsize=None)
def _path_flags() -> dict[str, str]:
    return {path: cli_flag(destination) for path, destination, _ in option_specs()}


def described(location: str) -> str:
    """A model location with the flag that sets it, so YAML and CLI users both recognize it."""
    flag = _path_flags().get(location.removeprefix("configuration."))
    return f"{location} ({flag})" if flag and location.startswith("configuration.") else location


def flatten_mapping(data, prefix=""):
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            yield from flatten_mapping(value, path)
        else:
            yield path


def set_mapping_path(mapping, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        if mapping.get(part) is None:
            mapping[part] = {}
        if not isinstance(mapping[part], dict):
            raise ConfigurationError(f"{part} must be a mapping")
        mapping = mapping[part]
    mapping[parts[-1]] = value


def config_value(config, path):
    for part in path.split("."):
        config = getattr(config, part)
    return config


def replace_path(config, path, value):
    head, *tail = path.split(".", 1)
    if tail:
        value = replace_path(getattr(config, head), tail[0], value)
    return replace(config, **{head: value})
