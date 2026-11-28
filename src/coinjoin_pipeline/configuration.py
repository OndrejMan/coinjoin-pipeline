"""Typed configuration of one experiment, decoded from its YAML file.

Each field is declared once, and its model path is its YAML key. Execution
consumes the model directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, is_dataclass, replace
from functools import lru_cache
from pathlib import Path
from types import NoneType, UnionType
from typing import Literal, Mapping, TypeVar, Union, get_args, get_origin, get_type_hints

from coinjoin_pipeline.execution.pbs.defaults import PBSMultijob, PBSScratchType
from coinjoin_pipeline.execution.stages import BlockSciWorkflow

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
    "external-analyze",
]
Engine = Literal["wasabi", "joinmarket"]
Driver = Literal["docker", "kubernetes"]
CoinjoinType = Literal["wasabi2", "joinmarket"]
ArtifactBackend = Literal["shared-storage", "s3"]
ContainerRuntime = Literal["docker", "podman"]
BlockSciTask = Literal["detect", "parse", "update", "script", "notebook", "external", "backup"]
BlockSciNetwork = Literal["bitcoin", "bitcoin_testnet", "bitcoin_regtest"]
JoinMarketDetector = Literal["possible", "definite"]
MappingMode = Literal["numeric", "all"]
AnalysisAction = Literal["collect_docker", "analyze_only"]
ExternalNetwork = Literal["bitcoin"]
ImagePullPolicy = Literal["Always", "IfNotPresent", "Never"]


def option(default=None, *, minimum=None, actions=None):
    """One configuration field (help text lives in arguments.py)."""
    # Called only inside the dataclass bodies below; pylint cannot see through the helper.
    return field(  # pylint: disable=invalid-field-call
        default=default, metadata={"minimum": minimum, "actions": actions}
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
    namespace: str = option("coinjoin")
    reuse_namespace: bool = option(False)
    kubeconfig: str | None = option(None)
    image_prefix: str = option("ghcr.io/ondrejman/")
    btc_datadir: str | None = option(None)
    copy_to_host: bool = option(False)
    infrastructure_local_build: bool = option(False)
    # Overrides the S3 emulation Job forwards to its controller; the emulator's defaults apply when unset.
    distributor_startup_timeout: int | None = option(None, minimum=1)
    btc_node_image: str | None = option(None)
    btc_node_initial_block_count: int | None = option(None, minimum=0)
    image_pull_policy: ImagePullPolicy | None = option(None)
    # Who launched the run, as the coinjoin.owner label of its Kubernetes objects.
    owner: str | None = option(None)
    # The run's peak quota as JSON, kept on the Job so any Lab reserves it before the pods exist.
    planned_quota: str | None = option(None)


@dataclass(frozen=True, slots=True)
class ArtifactConfiguration(ConfigurationModel):
    backend: ArtifactBackend = option("shared-storage")
    uri: str | None = option(None)
    endpoint_url: str | None = option(None)
    secret_name: str | None = option(None)
    credentials_file: str | None = option(None)
    profile: str | None = option(None)


@dataclass(frozen=True, slots=True)
class ImageConfiguration(ConfigurationModel):
    version: str | None = option(None)
    registry: str | None = option(None)
    local_build: bool = option(False)
    emulator: str | None = option(None)
    coinjoin_analysis: str | None = option(None)
    blocksci: str | None = option(None)
    mappings: str | None = option(None)
    sake: str | None = option(None)
    uploader: str | None = option(None)
    unified_report: str | None = option(None)


@dataclass(frozen=True, slots=True)
class BlockSciConfiguration(ConfigurationModel):
    workflow: BlockSciWorkflow = option("combined")
    task: BlockSciTask = option("detect")
    script: str | None = option(None)
    cache_source_run_id: str | None = option(None)
    notebook_port: int = option(8888, minimum=1)
    notebooks_dir: str | None = option(None)
    external_bitcoin_datadir: str | None = option(None)
    bitcoin_blocks_uri: str | None = option(None)
    external_blocksci_dir: str | None = option(None)
    external_baseline_uri: str | None = option(None)
    # Curated Dumplings false positives (coinjoin-analysis data/<protocol>/false_cjtxs.json).
    external_false_cjtxs_uri: str | None = option(None)
    network: BlockSciNetwork | None = option(None)
    max_block: int | None = option(None, minimum=0)
    expected_block_hash: str | None = option(None)
    # Script task only: the last height the analysis may see (Blockchain(config, pin_height + 1))
    # and the half-open range [scan_start_height, scan_end_height) it scans.
    pin_height: int | None = option(None, minimum=0)
    scan_start_height: int | None = option(None, minimum=0)
    scan_end_height: int | None = option(None, minimum=1)
    # A persistent index in place of the S3 cache: the working copy and the S3 prefix of
    # its single backup (written by backup, read to restore it).
    index_dir: str | None = option(None)
    index_backup_uri: str | None = option(None)
    # Fixed S3 prefix of the index jobs (parse, update, backup) in place of runs/<run-id>/.
    index_state_uri: str | None = option(None)
    # A reading job restores the backup into its own scratch instead of reading index_dir;
    # with restore_index it fills an incomplete index_dir instead and leaves the copy there.
    # dumplings_uri adds the Dumplings Scanner/ copy next to the index.
    index_in_scratch: bool = option(False)
    restore_index: bool = option(False)
    dumplings_uri: str | None = option(None)
    # A notebook over index_dir in place reads this Scanner/ in place too.
    dumplings_dir: str | None = option(None)
    # With restore_index: S3 object where the job reports the size of the kept copy.
    storage_report_uri: str | None = option(None)


@dataclass(frozen=True, slots=True)
class JoinMarketConfiguration(ConfigurationModel):
    detector: JoinMarketDetector = option("definite")
    min_base_fee: int = option(5000, minimum=0)
    percentage_fee: float = option(4e-05, minimum=0)
    max_depth: int = option(200000, minimum=1)


@dataclass(frozen=True, slots=True)
class MappingsConfiguration(ConfigurationModel):
    mining_fee_rate: int = option(1, minimum=0)
    coordination_fee_rate: float = option(0.003, minimum=0)
    max_decomposition_fee: int = option(6000, minimum=0)
    mode: MappingMode = option("numeric")
    timeout: int = option(60, minimum=1)
    retry_timeout: int = option(600, minimum=1)
    sake_seed: int = option(20260704, minimum=0)


@dataclass(frozen=True, slots=True)
class ExternalAnalysisConfiguration(ConfigurationModel):
    bitcoin_datadir: str | None = option(None)
    baseline: str | None = option(None)
    false_cjtxs: tuple[str, ...] = option(())
    network: ExternalNetwork | None = option(None)
    min_free_gb: int | None = option(None, minimum=0)
    resume: bool = option(False)

    @property
    def configured(self) -> bool:
        return any_option_set(self)


@dataclass(frozen=True, slots=True)
class StageConfiguration(ConfigurationModel):
    analysis: bool = option(False)
    blocksci: bool = option(False)
    mappings: bool = option(False)


@dataclass(frozen=True, slots=True)
class PBSResourceConfiguration(ConfigurationModel):
    ncpus: int | None = option(None, minimum=1)
    mem: str | None = option(None)
    scratch: str | None = option(None)
    scratch_type: PBSScratchType | None = option(None)
    walltime: str | None = option(None)
    # MetaCentrum multijob scratch that outlives one job; only the BlockSci stage uses it.
    multijob: PBSMultijob | None = option(None)

    @property
    def configured(self) -> bool:
        return any_option_set(self)


@dataclass(frozen=True, slots=True)
class PBSConfiguration(ConfigurationModel):
    ncpus: int | None = option(None, minimum=1)
    mem: str | None = option(None)
    scratch: str | None = option(None)
    scratch_type: PBSScratchType | None = option(None)
    walltime: str | None = option(None)
    image: str | None = option(None)
    blocksci_image: str | None = option(None)
    coinjoin_analysis_image: str | None = option(None)
    mappings_enumerator_image: str | None = option(None)
    sake_image: str | None = option(None)
    bitcoin_datadir: str | None = option(None)
    analysis: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    blocksci: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    mappings: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    unified_report: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    producer: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    restore: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    checkpoint: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    pack: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    clear: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)


@dataclass(frozen=True, slots=True)
class MainnetSyncConfiguration(ConfigurationModel):
    target_height: int | None = option(None, minimum=1)
    network: Literal["bitcoin", "bitcoin_regtest"] = option("bitcoin")
    scan_start_height: int | None = option(None, minimum=1)
    root: str | None = option(None)
    # Fixed S3 prefix of the synchronization's state, markers and per-run logs.
    state_uri: str | None = option(None)
    backup_uri: str | None = option(None)
    blocks_uri: str | None = option(None)
    dumplings_uri: str | None = option(None)
    core_image: str = option("ghcr.io/ondrejman/bitcoin-block-archive:latest")
    dumplings_image: str = option("ghcr.io/ondrejman/dumplings:latest")
    dbcache_mb: int = option(4096, minimum=1)
    reorg_reserve: int = option(2016, minimum=288)
    poll_seconds: int = option(30, minimum=1)
    # Empty the working dataset after the last job; the next restore job refills it from S3.
    clear_after_sync: bool = option(False)
    # Run ID of a Dumplings baseline that brings the CoinJoin Explorer data up to this
    # synchronization; it waits only for the checkpoint, which publishes the Dumplings copy.
    explorer_run_id: str | None = option(None)
    # Kept notebook data beside the dataset; only measured, so the storage report covers it.
    notebook_root: str | None = option(None)


@dataclass(frozen=True, slots=True)
class ResearchAnalysisConfiguration(ConfigurationModel):
    txid: str | None = option(None)
    tx_hex: str | None = option(None)
    transaction_file: str | None = option(None)
    input_indices: tuple[int, ...] = option(())
    output_indices: tuple[int, ...] = option(())
    allow_network: bool = option(False)
    min_wallets: int = option(1, minimum=1)
    depth: int = option(100, minimum=1)
    permute_inputs: bool = option(False)
    permute_outputs: bool = option(False)
    force_limit_anon_score: float = option(100.0, minimum=0)
    plot: bool = option(False)
    coordinator_file: str | None = option(None)


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
    ) = option(None)
    actions: tuple[str, ...] = option(())
    cjtype: Literal["ww1", "ww2", "sw", "jm"] = option("ww2")
    target_paths: tuple[str, ...] = option(())
    load_config: str | None = option(None)
    env_vars: str | None = option(None)
    input_uri: str | None = option(None)
    input_dir: str | None = option(None)
    filename: str | None = option(None)
    data_mode: Literal["emulation", "mainnet"] = option("emulation")
    research: ResearchAnalysisConfiguration = field(default_factory=ResearchAnalysisConfiguration)


@dataclass(frozen=True, slots=True)
class PipelineConfiguration(ConfigurationModel):
    action: PipelineAction = option("full-run")
    runtime: ContainerRuntime = option("docker")
    runs_root: str | None = option(None)
    engine: Engine = option("wasabi")
    coinjoin_type: CoinjoinType = option("wasabi2")
    driver: Driver = option("docker")
    scenario: str | None = option(None)
    run_dir: str | None = option(None, actions=("analyze", "mappings", "export", "coinjoin-analysis"))
    run_id: str | None = option(
        None, actions=("emulate", "full-run", "pbs-from-s3", "mainnet-sync", "external-analyze")
    )
    run_timezone: str = option("Europe/Prague")
    min_input_count: int | None = option(None, minimum=1)
    dry_run: bool = option(False)
    parallel: bool = option(False, actions=("full-run",))
    resume_run: bool = option(False, actions=("full-run",))
    all_runs: bool = option(False, actions=("coinjoin-analysis",))
    yes: bool = option(False, actions=("clean",))
    analysis_action: AnalysisAction = option("collect_docker", actions=("coinjoin-analysis",))
    # Mainnet baseline: the S3 prefix holding the Dumplings Scanner/ copy and its manifest.json.
    dumplings_uri: str | None = option(None)
    # With the baseline: the fixed prefix of the CoinJoin Explorer data the job brings up to date.
    explorer_uri: str | None = option(None)
    # CoinJoin Explorer's cj_browser directory on this host; the job then also renders its offline page.
    explorer_source: str | None = option(None)
    emulation_timeout: int = option(21600, minimum=1)
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
    stage_exporters: bool = option(False)
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
    def from_paths(cls, values: Mapping[str, object]):
        """A configuration from YAML paths such as ``artifacts.uri``, as command-line overrides give them."""
        mapping: dict[str, object] = {}
        for path, value in values.items():
            set_mapping_path(mapping, path, value)
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


def without_none(annotation):
    """``X`` of ``X | None``; any other annotation unchanged."""
    if get_origin(annotation) in (Union, UnionType):
        return next(item for item in get_args(annotation) if item is not NoneType)
    return annotation


def decode_value(annotation, value, location):
    if is_dataclass(annotation):
        return decode_model(annotation, value, location)
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        if value is None and NoneType in arguments:
            return None
        return decode_value(without_none(annotation), value, location)
    if origin is Literal:
        if value not in arguments or isinstance(value, bool):
            raise ConfigurationError(f"{location} must be one of: {', '.join(map(str, arguments))}")
        return value
    if origin is tuple:
        if arguments[0] is not str:
            if not isinstance(value, (list, tuple)):
                raise ConfigurationError(f"{location} must be a list")
            return tuple(decode_value(arguments[0], item, f"{location}[{index}]") for index, item in enumerate(value))
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
    """Yield (model path, field) for every setting the model declares."""
    annotations = model_annotations(model_type)
    for item in fields(model_type):
        if item.name.startswith("_") or item.name == "action":
            continue
        path = f"{prefix}.{item.name}" if prefix else item.name
        annotation = annotations[item.name]
        if is_dataclass(annotation):
            yield from option_specs(annotation, path)
        else:
            yield path, item


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
