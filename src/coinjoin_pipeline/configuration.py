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
    "mappings",
    "pbs-from-s3",
    "initialize",
    "clean",
    "external analyze",
]
Engine = Literal["wasabi", "joinmarket"]
Driver = Literal["docker", "kubernetes"]
CoinjoinType = Literal["wasabi2", "joinmarket"]
ArtifactBackend = Literal["shared-storage", "s3"]
ContainerRuntime = Literal["docker", "podman"]
BlockSciWorkflow = Literal["combined", "reusable", "cached"]
BlockSciTask = Literal["detect", "parse", "update", "script", "notebook", "external"]
BlockSciNetwork = Literal["bitcoin", "bitcoin_testnet", "bitcoin_regtest"]
JoinMarketDetector = Literal["possible", "definite"]
MappingMode = Literal["numeric", "all"]
AnalysisAction = Literal["collect_docker", "analyze_only"]
ExternalNetwork = Literal["bitcoin"]
YAML_ACTION_ALIASES = {"external-analyze": "external analyze"}


def option(default=None, *, name=None, minimum=None, actions=None):
    # Called only inside the dataclass bodies below; pylint cannot see through the helper.
    return field(  # pylint: disable=invalid-field-call
        default=default, metadata={"option": name, "minimum": minimum, "actions": actions}
    )


class ConfigurationModel:
    @classmethod
    def from_mapping(cls, value, location=None):
        return decode_model(cls, value, location or cls.__name__)


@dataclass(frozen=True, slots=True)
class KubernetesConfiguration(ConfigurationModel):
    namespace: str = option("coinjoin", name="namespace")
    reuse_namespace: bool = option(False, name="reuse_namespace")
    kubeconfig: str | None = option(None, name="kubeconfig")
    image_prefix: str = option("ghcr.io/ondrejman/", name="image_prefix")
    btc_datadir: str | None = option(None, name="kubernetes_btc_datadir")
    copy_to_host: bool = option(False, name="copy_to_host")
    infrastructure_local_build: bool = option(False, name="coinjoin_infrastructure_local_build")


@dataclass(frozen=True, slots=True)
class ArtifactConfiguration(ConfigurationModel):
    backend: ArtifactBackend = option("shared-storage", name="artifact_backend")
    uri: str | None = option(None, name="artifact_uri")
    endpoint_url: str | None = option(None, name="s3_endpoint_url")
    secret_name: str | None = option(None, name="s3_secret_name")
    credentials_file: str | None = option(None, name="s3_credentials_file")
    profile: str | None = option(None, name="s3_profile")


@dataclass(frozen=True, slots=True)
class ImageConfiguration(ConfigurationModel):
    version: str | None = option(None, name="version")
    local_build: bool = option(False, name="local_build")
    emulator: str | None = option(None, name="emulator_image")
    coinjoin_analysis: str | None = option(None, name="coinjoin_analysis_image")
    blocksci: str | None = option(None, name="blocksci_image")
    mappings: str | None = option(None, name="mappings_image")
    sake: str | None = option(None, name="sake_image")
    uploader: str | None = option(None, name="uploader_image")
    unified_report: str | None = option(None, name="unified_report_image")


@dataclass(frozen=True, slots=True)
class BlockSciConfiguration(ConfigurationModel):
    workflow: BlockSciWorkflow = option("combined", name="blocksci_workflow")
    task: BlockSciTask = option("detect", name="blocksci_task")
    script: str | None = option(None, name="blocksci_script")
    cache_source_run_id: str | None = option(None, name="blocksci_cache_source_run_id")
    notebook_port: int | None = option(None, name="blocksci_notebook_port", minimum=1)
    notebooks_dir: str | None = option(None, name="blocksci_notebooks_dir")
    external_bitcoin_datadir: str | None = option(None, name="blocksci_external_bitcoin_datadir")
    bitcoin_blocks_uri: str | None = option(None, name="blocksci_bitcoin_blocks_uri")
    external_blocksci_dir: str | None = option(None, name="blocksci_external_blocksci_dir")
    external_baseline_uri: str | None = option(None, name="external_baseline_uri")
    network: BlockSciNetwork | None = option(None, name="blocksci_network")
    max_block: int | None = option(None, name="blocksci_max_block", minimum=0)


@dataclass(frozen=True, slots=True)
class JoinMarketConfiguration(ConfigurationModel):
    detector: JoinMarketDetector = option("definite", name="joinmarket_detector")
    min_base_fee: int = option(5000, name="joinmarket_min_base_fee", minimum=0)
    percentage_fee: float = option(4e-05, name="joinmarket_percentage_fee", minimum=0)
    max_depth: int = option(200000, name="joinmarket_max_depth", minimum=1)


@dataclass(frozen=True, slots=True)
class MappingsConfiguration(ConfigurationModel):
    mining_fee_rate: int = option(1, name="mapping_mining_fee_rate", minimum=0)
    coordination_fee_rate: float = option(0.003, name="mapping_coordination_fee_rate", minimum=0)
    max_decomposition_fee: int = option(6000, name="mapping_max_decomposition_fee", minimum=0)
    mode: MappingMode = option("numeric", name="mapping_mode")
    timeout: int = option(60, name="mapping_timeout", minimum=1)
    retry_timeout: int = option(600, name="mapping_retry_timeout", minimum=1)
    sake_seed: int = option(20260704, name="sake_seed", minimum=0)


@dataclass(frozen=True, slots=True)
class ExternalAnalysisConfiguration(ConfigurationModel):
    bitcoin_datadir: str | None = option(None, name="bitcoin_datadir")
    baseline: str | None = option(None, name="baseline")
    false_cjtxs: tuple[str, ...] = option((), name="false_cjtxs")
    network: ExternalNetwork | None = option(None, name="network")
    min_free_gb: int | None = option(None, name="min_free_gb", minimum=0)
    resume: bool = option(False, name="resume")

    @property
    def configured(self) -> bool:
        return any(
            getattr(self, item.name) is not None
            and getattr(self, item.name) is not False
            and getattr(self, item.name) != ()
            for item in fields(self)
        )


@dataclass(frozen=True, slots=True)
class StageConfiguration(ConfigurationModel):
    analysis: bool = option(False, name="analysisPbs")
    blocksci: bool = option(False, name="blocksciPbs")
    mappings: bool = option(False, name="mappingsPbs")


@dataclass(frozen=True, slots=True)
class PBSResourceConfiguration(ConfigurationModel):
    ncpus: int | None = option(None, name="ncpus", minimum=1)
    mem: str | None = option(None, name="mem")
    scratch: str | None = option(None, name="scratch")
    walltime: str | None = option(None, name="walltime")

    @property
    def configured(self) -> bool:
        return any(
            getattr(self, item.name) is not None
            and getattr(self, item.name) is not False
            and getattr(self, item.name) != ()
            for item in fields(self)
        )


@dataclass(frozen=True, slots=True)
class PBSConfiguration(ConfigurationModel):
    ncpus: int | None = option(None, name="pbs_ncpus", minimum=1)
    mem: str | None = option(None, name="pbs_mem")
    scratch: str | None = option(None, name="pbs_scratch")
    walltime: str | None = option(None, name="pbs_walltime")
    image: str | None = option(None, name="pbs_image")
    blocksci_image: str | None = option(None, name="pbs_blocksci_image")
    coinjoin_analysis_image: str | None = option(None, name="pbs_coinjoin_analysis_image")
    mappings_enumerator_image: str | None = option(None, name="pbs_mappings_enumerator_image")
    sake_image: str | None = option(None, name="pbs_sake_image")
    bitcoin_datadir: str | None = option(None, name="pbs_bitcoin_datadir")
    analysis: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    blocksci: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    mappings: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    unified_report: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)


@dataclass(frozen=True, slots=True)
class PipelineConfiguration(ConfigurationModel):
    action: PipelineAction = option("full-run")
    runtime: ContainerRuntime = option("docker", name="runtime")
    runs_root: str | None = option(None, name="runs_root")
    engine: Engine = option("wasabi", name="engine")
    coinjoin_type: CoinjoinType = option("wasabi2", name="coinjoin_type")
    driver: Driver = option("docker", name="driver")
    scenario: str | None = option(None, name="scenario")
    run_dir: str | None = option(None, name="run_dir", actions=("analyze", "mappings", "export", "coinjoin-analysis"))
    run_id: str | None = option(None, name="run_id", actions=("emulate", "full-run", "pbs-from-s3", "external analyze"))
    run_timezone: str = option("Europe/Prague", name="run_timezone")
    min_input_count: int | None = option(None, name="min_input_count", minimum=1)
    dry_run: bool = option(False, name="dry_run")
    parallel: bool = option(False, name="parallel", actions=("full-run",))
    all_runs: bool = option(False, name="all_runs", actions=("coinjoin-analysis",))
    yes: bool = option(False, name="yes", actions=("clean",))
    analysis_action: AnalysisAction = option("collect_docker", name="analysis_action", actions=("coinjoin-analysis",))
    emulation_timeout: int = option(21600, name="emulation_timeout", minimum=1)
    kubernetes: KubernetesConfiguration = field(default_factory=KubernetesConfiguration)
    artifacts: ArtifactConfiguration = field(default_factory=ArtifactConfiguration)
    images: ImageConfiguration = field(default_factory=ImageConfiguration)
    blocksci: BlockSciConfiguration = field(default_factory=BlockSciConfiguration)
    joinmarket: JoinMarketConfiguration = field(default_factory=JoinMarketConfiguration)
    mappings: MappingsConfiguration = field(default_factory=MappingsConfiguration)
    external: ExternalAnalysisConfiguration = field(default_factory=ExternalAnalysisConfiguration)
    stages: StageConfiguration = field(default_factory=StageConfiguration)
    pbs: PBSConfiguration = field(default_factory=PBSConfiguration)
    stage_exporters: bool = option(False, name="stage_exporters")
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
        raw_action = data.get("action", "full-run")
        data["action"] = YAML_ACTION_ALIASES.get(raw_action, raw_action) if isinstance(raw_action, str) else raw_action
        model = decode_model(cls, data, "configuration")
        provided = frozenset(flatten_mapping(data))
        return replace(model, _provided=provided)

    @classmethod
    def from_flat(cls, values: Mapping[str, object]):
        mapping: dict[str, object] = {}
        routes = {dest: path for path, dest, _ in option_specs()}
        routes["action"] = "action"
        for dest, value in values.items():
            if dest not in routes:
                raise ConfigurationError(f"unsupported option: {dest}")
            set_mapping_path(mapping, routes[dest], value)
        return cls.from_mapping(mapping)

    def validate(self) -> None:
        from .option_rules import cross_option_errors

        errors = cross_option_errors(self)
        for path, destination, item in option_specs():
            permitted = item.metadata.get("actions")
            if permitted and path in self._provided and self.action not in permitted:
                value = config_value(self, path)
                if value is not None and value is not False:
                    errors.append(f"--{destination.replace('_', '-')} is not supported for {self.action}")
        if self.action == "external analyze" and not self.run_id:
            errors.append("external analyze requires --run-id")
        if self.all_runs and self.stages.analysis:
            errors.append("--all-runs is not supported with --analysisPbs; select one run")
        if not 1024 <= (self.blocksci.notebook_port or 8888) <= 65535:
            errors.append("--blocksci-notebook-port must be between 1024 and 65535")
        if (
            self.artifacts.backend == "s3"
            and self.action != "pbs-from-s3"
            and (self.kubernetes.btc_datadir or self.pbs.bitcoin_datadir or self.kubernetes.copy_to_host)
        ):
            errors.append("Kubernetes S3-compatible mode does not support shared Bitcoin storage or --copy-to-host")
        if self.driver == "kubernetes" and self.stages.blocksci and not self.kubernetes.copy_to_host:
            first, second = self.kubernetes.btc_datadir, self.pbs.bitcoin_datadir
            if first and second and Path(first).expanduser().resolve() != Path(second).expanduser().resolve():
                errors.append("Kubernetes and PBS Bitcoin datadirs must identify the same directory")

        if self.action != "external analyze" and self.external.configured:
            errors.append("external settings require action: external analyze")
        if self.action == "external analyze":
            supplied = self.external.bitcoin_datadir is not None or self.external.baseline is not None
            if self.external.resume and supplied:
                errors.append("external.resume cannot be combined with external.bitcoin_datadir or external.baseline")
            if not self.external.resume and (not self.external.bitcoin_datadir or not self.external.baseline):
                errors.append("a new external analysis requires external.bitcoin_datadir and external.baseline")
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
            raise ConfigurationError(f"{location} must be one of: {', '.join(map(str, arguments))}")
        return value
    if origin is tuple:
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, (list, tuple)) or not all(isinstance(item, str) for item in values):
            raise ConfigurationError(f"{location} must be a string or a list of strings")
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
        raise ConfigurationError(f"{location} must be {expected}")
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
            raise ConfigurationError(f"{location}.{key} must be {label}")
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
            if model_type is PBSResourceConfiguration:
                destination = path.replace(".", "_")
            yield path, destination, item


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
