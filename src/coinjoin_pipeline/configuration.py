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
    "mainnet-sync",
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
BlockSciTask = Literal["detect", "parse", "update", "script", "notebook", "external", "backup"]
BlockSciNetwork = Literal["bitcoin", "bitcoin_testnet", "bitcoin_regtest"]
JoinMarketDetector = Literal["possible", "definite"]
MappingMode = Literal["numeric", "all"]
AnalysisAction = Literal["collect_docker", "analyze_only"]
ExternalNetwork = Literal["bitcoin"]
PBSScratchType = Literal["ssd", "local"]
PBSMultijob = Literal["brno", "plzen", "global"]
# Alias -> canonical action, accepted from YAML and the command line alike.
# Aliases are never named in error text.
ACTION_ALIASES = {"coinjoin": "coinjoin-analysis", "external-analyze": "external analyze"}


def option(default=None, *, name=None, minimum=None, actions=None):
    """One configuration field; ``name`` is its CLI destination (help text lives in arguments.py)."""
    # Called only inside the dataclass bodies below; pylint cannot see through the helper.
    return field(  # pylint: disable=invalid-field-call
        default=default, metadata={"option": name, "minimum": minimum, "actions": actions}
    )


def any_option_set(model) -> bool:
    """Whether any field holds a value other than null, false or an empty list."""
    return any(
        (value := getattr(model, item.name)) is not None and value is not False and value != ()
        for item in fields(model)
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
    notebook_port: int = option(8888, name="blocksci_notebook_port", minimum=1)
    notebooks_dir: str | None = option(None, name="blocksci_notebooks_dir")
    external_bitcoin_datadir: str | None = option(None, name="blocksci_external_bitcoin_datadir")
    bitcoin_blocks_uri: str | None = option(None, name="blocksci_bitcoin_blocks_uri")
    external_blocksci_dir: str | None = option(None, name="blocksci_external_blocksci_dir")
    external_baseline_uri: str | None = option(None, name="external_baseline_uri")
    # Curated Dumplings false positives (coinjoin-analysis data/<protocol>/false_cjtxs.json).
    external_false_cjtxs_uri: str | None = option(None, name="external_false_cjtxs_uri")
    network: BlockSciNetwork | None = option(None, name="blocksci_network")
    max_block: int | None = option(None, name="blocksci_max_block", minimum=0)
    expected_block_hash: str | None = option(None, name="blocksci_expected_block_hash")
    # Script task only: the last height the analysis may see (Blockchain(config, pin_height + 1))
    # and the half-open range [scan_start_height, scan_end_height) it scans.
    pin_height: int | None = option(None, name="blocksci_pin_height", minimum=0)
    scan_start_height: int | None = option(None, name="blocksci_scan_start_height", minimum=0)
    scan_end_height: int | None = option(None, name="blocksci_scan_end_height", minimum=1)
    # A persistent index in place of the S3 cache: the working copy and the S3 prefix of
    # its single backup (written by backup, read to restore it).
    index_dir: str | None = option(None, name="blocksci_index_dir")
    index_backup_uri: str | None = option(None, name="blocksci_index_backup_uri")
    # Fixed S3 prefix of the index jobs (parse, update, backup) in place of runs/<run-id>/.
    index_state_uri: str | None = option(None, name="blocksci_index_state_uri")


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
        return any_option_set(self)


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
    scratch_type: PBSScratchType | None = option(None, name="scratch_type")
    walltime: str | None = option(None, name="walltime")
    # MetaCentrum multijob scratch that outlives one job; only the BlockSci stage uses it.
    multijob: PBSMultijob | None = option(None, name="multijob")

    @property
    def configured(self) -> bool:
        return any_option_set(self)


@dataclass(frozen=True, slots=True)
class PBSConfiguration(ConfigurationModel):
    ncpus: int | None = option(None, name="pbs_ncpus", minimum=1)
    mem: str | None = option(None, name="pbs_mem")
    scratch: str | None = option(None, name="pbs_scratch")
    scratch_type: PBSScratchType | None = option(None, name="pbs_scratch_type")
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
    producer: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    restore: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    checkpoint: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)


@dataclass(frozen=True, slots=True)
class MainnetSyncConfiguration(ConfigurationModel):
    target_height: int | None = option(None, name="mainnet_target_height", minimum=1)
    network: Literal["bitcoin", "bitcoin_regtest"] = option("bitcoin", name="mainnet_network")
    scan_start_height: int | None = option(None, name="mainnet_scan_start_height", minimum=1)
    root: str | None = option(None, name="mainnet_root")
    # Fixed S3 prefix of the synchronization's state, markers and per-run logs.
    state_uri: str | None = option(None, name="mainnet_state_uri")
    backup_uri: str | None = option(None, name="mainnet_backup_uri")
    blocks_uri: str | None = option(None, name="mainnet_blocks_uri")
    dumplings_uri: str | None = option(None, name="mainnet_dumplings_uri")
    core_image: str = option("ghcr.io/ondrejman/bitcoin-block-archive:latest", name="mainnet_core_image")
    dumplings_image: str = option("ghcr.io/ondrejman/dumplings:latest", name="mainnet_dumplings_image")
    dbcache_mb: int = option(8192, name="mainnet_dbcache_mb", minimum=1)
    reorg_reserve: int = option(2016, name="mainnet_reorg_reserve", minimum=288)
    poll_seconds: int = option(30, name="mainnet_poll_seconds", minimum=1)


@dataclass(frozen=True, slots=True)
class ResearchAnalysisConfiguration(ConfigurationModel):
    txid: str | None = option(None, name="analysis_txid")
    tx_hex: str | None = option(None, name="analysis_tx_hex")
    transaction_file: str | None = option(None, name="analysis_transaction_file")
    input_indices: tuple[int, ...] = option((), name="analysis_input_index")
    output_indices: tuple[int, ...] = option((), name="analysis_output_index")
    allow_network: bool = option(False, name="analysis_allow_network")
    min_wallets: int = option(1, name="analysis_min_wallets", minimum=1)
    depth: int = option(100, name="analysis_depth", minimum=1)
    permute_inputs: bool = option(False, name="analysis_permute_inputs")
    permute_outputs: bool = option(False, name="analysis_permute_outputs")
    force_limit_anon_score: float = option(100.0, name="analysis_force_limit_anon_score", minimum=0)
    plot: bool = option(False, name="analysis_plot")
    coordinator_file: str | None = option(None, name="analysis_coordinator_file")


@dataclass(frozen=True, slots=True)
class CoinjoinAnalysisConfiguration(ConfigurationModel):
    program: (
        Literal[
            "parse_cj_logs",
            "parse_dumplings",
            "ww2_analyze_client",
            "scan_results_plot",
            "file_check",
            "visualize_coordinators",
            "perf_processing",
            "analyze_tx_size",
            "generate_llm_inputs",
            "update_txids_with_wabisator",
            "coinjoin_statistics",
        ]
        | None
    ) = option(None, name="analysis_program")
    actions: tuple[str, ...] = option((), name="analysis_operation")
    cjtype: Literal["ww1", "ww2", "sw", "jm"] = option("ww2", name="analysis_cjtype")
    target_paths: tuple[str, ...] = option((), name="analysis_target_path")
    load_config: str | None = option(None, name="analysis_load_config")
    env_vars: str | None = option(None, name="analysis_env_vars")
    input_uri: str | None = option(None, name="analysis_input_uri")
    input_dir: str | None = option(None, name="analysis_input_dir")
    filename: str | None = option(None, name="analysis_filename")
    data_mode: Literal["emulation", "mainnet"] = option("emulation", name="analysis_data_mode")
    research: ResearchAnalysisConfiguration = field(default_factory=ResearchAnalysisConfiguration)


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
    run_id: str | None = option(
        None, name="run_id", actions=("emulate", "full-run", "pbs-from-s3", "mainnet-sync", "external analyze")
    )
    run_timezone: str = option("Europe/Prague", name="run_timezone")
    min_input_count: int | None = option(None, name="min_input_count", minimum=1)
    dry_run: bool = option(False, name="dry_run")
    parallel: bool = option(False, name="parallel", actions=("full-run",))
    all_runs: bool = option(False, name="all_runs", actions=("coinjoin-analysis",))
    yes: bool = option(False, name="yes", actions=("clean",))
    analysis_action: AnalysisAction = option("collect_docker", name="analysis_action", actions=("coinjoin-analysis",))
    # Mainnet baseline: the S3 prefix holding the Dumplings Scanner/ copy and its manifest.json.
    dumplings_uri: str | None = option(None, name="dumplings_uri")
    emulation_timeout: int = option(21600, name="emulation_timeout", minimum=1)
    kubernetes: KubernetesConfiguration = field(default_factory=KubernetesConfiguration)
    artifacts: ArtifactConfiguration = field(default_factory=ArtifactConfiguration)
    images: ImageConfiguration = field(default_factory=ImageConfiguration)
    coinjoin_analysis: CoinjoinAnalysisConfiguration = field(default_factory=CoinjoinAnalysisConfiguration)
    blocksci: BlockSciConfiguration = field(default_factory=BlockSciConfiguration)
    mainnet: MainnetSyncConfiguration = field(default_factory=MainnetSyncConfiguration)
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
        data["action"] = ACTION_ALIASES.get(raw_action, raw_action) if isinstance(raw_action, str) else raw_action
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
            if model_type is PBSResourceConfiguration:
                destination = path.replace(".", "_")
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
