"""Typed YAML experiment configuration translated to the public CLI contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, TypeVar, cast

import yaml


class ConfigurationError(ValueError):
    """Raised when a YAML configuration cannot be translated safely."""


PipelineAction = Literal[
    "full-run",
    "recreate",
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
BlockSciTask = Literal["detect", "parse", "update", "script", "notebook"]
BlockSciNetwork = Literal["bitcoin", "bitcoin_testnet", "bitcoin_regtest"]
JoinMarketDetector = Literal["possible", "definite"]
MappingMode = Literal["numeric", "all"]
AnalysisAction = Literal["collect_docker", "analyze_only"]
ExternalNetwork = Literal["bitcoin"]

KNOWN_ACTIONS = {
    "full-run",
    "recreate",
    "analyze",
    "export",
    "coinjoin-analysis",
    "mappings",
    "pbs-from-s3",
    "initialize",
    "clean",
    "external analyze",
}
YAML_ACTION_ALIASES = {"external-analyze": "external analyze"}
PBS_RESOURCES = ("ncpus", "mem", "scratch", "walltime")
_Choice = TypeVar("_Choice", bound=str)


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{location} must be a mapping")
    return value


def _reject_unknown(
    mapping: Mapping[str, Any], allowed: set[str], location: str
) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise ConfigurationError(f"unsupported {location} key(s): {', '.join(unknown)}")


def _optional_string(mapping: Mapping[str, Any], key: str, location: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError(f"{location}.{key} must be a string")
    return value


def _string_tuple(
    mapping: Mapping[str, Any], key: str, location: str
) -> tuple[str, ...]:
    value = mapping.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(
            f"{location}.{key} must be a string or a list of strings"
        )
    return tuple(value)


def _optional_positive_int(
    mapping: Mapping[str, Any], key: str, location: str
) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigurationError(f"{location}.{key} must be a positive integer")
    return value


def _optional_non_negative_int(
    mapping: Mapping[str, Any], key: str, location: str
) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationError(f"{location}.{key} must be a non-negative integer")
    return value


def _optional_non_negative_float(
    mapping: Mapping[str, Any], key: str, location: str
) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigurationError(f"{location}.{key} must be a non-negative number")
    return float(value)


def _boolean(
    mapping: Mapping[str, Any], key: str, location: str, *, default: bool = False
) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{location}.{key} must be true or false")
    return value


def _choice(
    mapping: Mapping[str, Any],
    key: str,
    location: str,
    choices: tuple[_Choice, ...],
    *,
    default: _Choice | None = None,
) -> _Choice | None:
    value = mapping.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or value not in choices:
        raise ConfigurationError(
            f"{location}.{key} must be one of: {', '.join(choices)}"
        )
    return cast(_Choice, value)


def _append_option(
    arguments: list[str], flag: str, value: str | int | float | None
) -> None:
    if value is not None:
        arguments.extend((flag, str(value)))


def _append_flag(arguments: list[str], flag: str, enabled: bool) -> None:
    if enabled:
        arguments.append(flag)


@dataclass(frozen=True, slots=True)
class KubernetesConfiguration:
    namespace: str | None = None
    reuse_namespace: bool = False
    kubeconfig: str | None = None
    image_prefix: str | None = None
    btc_datadir: str | None = None
    copy_to_host: bool = False
    infrastructure_local_build: bool = False

    @classmethod
    def from_mapping(cls, value: Any) -> KubernetesConfiguration:
        data = _mapping(value, "kubernetes")
        _reject_unknown(
            data,
            {
                "namespace",
                "reuse_namespace",
                "kubeconfig",
                "image_prefix",
                "btc_datadir",
                "copy_to_host",
                "infrastructure_local_build",
            },
            "kubernetes",
        )
        return cls(
            namespace=_optional_string(data, "namespace", "kubernetes"),
            reuse_namespace=_boolean(data, "reuse_namespace", "kubernetes"),
            kubeconfig=_optional_string(data, "kubeconfig", "kubernetes"),
            image_prefix=_optional_string(data, "image_prefix", "kubernetes"),
            btc_datadir=_optional_string(data, "btc_datadir", "kubernetes"),
            copy_to_host=_boolean(data, "copy_to_host", "kubernetes"),
            infrastructure_local_build=_boolean(
                data, "infrastructure_local_build", "kubernetes"
            ),
        )

    def append_arguments(self, arguments: list[str]) -> None:
        _append_option(arguments, "--namespace", self.namespace)
        _append_flag(arguments, "--reuse-namespace", self.reuse_namespace)
        _append_option(arguments, "--kubeconfig", self.kubeconfig)
        _append_option(arguments, "--image-prefix", self.image_prefix)
        _append_option(arguments, "--kubernetes-btc-datadir", self.btc_datadir)
        _append_flag(arguments, "--copy-to-host", self.copy_to_host)
        _append_flag(
            arguments,
            "--coinjoin-infrastructure-local-build",
            self.infrastructure_local_build,
        )


@dataclass(frozen=True, slots=True)
class ArtifactConfiguration:
    backend: ArtifactBackend | None = None
    uri: str | None = None
    endpoint_url: str | None = None
    secret_name: str | None = None
    credentials_file: str | None = None
    profile: str | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> ArtifactConfiguration:
        data = _mapping(value, "artifacts")
        _reject_unknown(
            data,
            {"backend", "uri", "endpoint_url", "secret_name", "credentials_file", "profile"},
            "artifacts",
        )
        return cls(
            backend=cast(
                ArtifactBackend | None,
                _choice(
                    data,
                    "backend",
                    "artifacts",
                    ("shared-storage", "s3"),
                ),
            ),
            uri=_optional_string(data, "uri", "artifacts"),
            endpoint_url=_optional_string(data, "endpoint_url", "artifacts"),
            secret_name=_optional_string(data, "secret_name", "artifacts"),
            credentials_file=_optional_string(data, "credentials_file", "artifacts"),
            profile=_optional_string(data, "profile", "artifacts"),
        )

    def append_arguments(self, arguments: list[str]) -> None:
        _append_option(arguments, "--artifact-backend", self.backend)
        _append_option(arguments, "--artifact-uri", self.uri)
        _append_option(arguments, "--s3-endpoint-url", self.endpoint_url)
        _append_option(arguments, "--s3-secret-name", self.secret_name)
        _append_option(arguments, "--s3-credentials-file", self.credentials_file)
        _append_option(arguments, "--s3-profile", self.profile)


@dataclass(frozen=True, slots=True)
class ImageConfiguration:
    version: str | None = None
    local_build: bool = False
    pipeline: str | None = None
    emulator: str | None = None
    coinjoin_analysis: str | None = None
    blocksci: str | None = None
    mappings: str | None = None
    sake: str | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> ImageConfiguration:
        data = _mapping(value, "images")
        _reject_unknown(
            data,
            {
                "version",
                "local_build",
                "pipeline",
                "emulator",
                "coinjoin_analysis",
                "blocksci",
                "mappings",
                "sake",
            },
            "images",
        )
        return cls(
            version=_optional_string(data, "version", "images"),
            local_build=_boolean(data, "local_build", "images"),
            pipeline=_optional_string(data, "pipeline", "images"),
            emulator=_optional_string(data, "emulator", "images"),
            coinjoin_analysis=_optional_string(
                data, "coinjoin_analysis", "images"
            ),
            blocksci=_optional_string(data, "blocksci", "images"),
            mappings=_optional_string(data, "mappings", "images"),
            sake=_optional_string(data, "sake", "images"),
        )

    def append_arguments(self, arguments: list[str]) -> None:
        _append_option(arguments, "--version", self.version)
        _append_flag(arguments, "--local-build", self.local_build)
        _append_option(arguments, "--pipeline-image", self.pipeline)
        _append_option(arguments, "--emulator-image", self.emulator)
        _append_option(
            arguments, "--coinjoin-analysis-image", self.coinjoin_analysis
        )
        _append_option(arguments, "--blocksci-image", self.blocksci)
        _append_option(arguments, "--mappings-image", self.mappings)
        _append_option(arguments, "--sake-image", self.sake)


@dataclass(frozen=True, slots=True)
class BlockSciConfiguration:
    workflow: BlockSciWorkflow | None = None
    task: BlockSciTask | None = None
    script: str | None = None
    cache_source_run_id: str | None = None
    notebook_port: int | None = None
    notebooks_dir: str | None = None
    external_bitcoin_datadir: str | None = None
    external_blocksci_dir: str | None = None
    network: BlockSciNetwork | None = None
    max_block: int | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> BlockSciConfiguration:
        data = _mapping(value, "blocksci")
        _reject_unknown(
            data,
            {
                "workflow",
                "task",
                "script",
                "cache_source_run_id",
                "notebook_port",
                "notebooks_dir",
                "external_bitcoin_datadir",
                "external_blocksci_dir",
                "network",
                "max_block",
            },
            "blocksci",
        )
        return cls(
            workflow=cast(
                BlockSciWorkflow | None,
                _choice(
                    data,
                    "workflow",
                    "blocksci",
                    ("combined", "reusable", "cached"),
                ),
            ),
            task=cast(
                BlockSciTask | None,
                _choice(
                    data,
                    "task",
                    "blocksci",
                    ("detect", "parse", "update", "script", "notebook"),
                ),
            ),
            script=_optional_string(data, "script", "blocksci"),
            cache_source_run_id=_optional_string(
                data, "cache_source_run_id", "blocksci"
            ),
            notebook_port=_optional_positive_int(
                data, "notebook_port", "blocksci"
            ),
            notebooks_dir=_optional_string(data, "notebooks_dir", "blocksci"),
            external_bitcoin_datadir=_optional_string(
                data, "external_bitcoin_datadir", "blocksci"
            ),
            external_blocksci_dir=_optional_string(
                data, "external_blocksci_dir", "blocksci"
            ),
            network=cast(
                BlockSciNetwork | None,
                _choice(
                    data,
                    "network",
                    "blocksci",
                    ("bitcoin", "bitcoin_testnet", "bitcoin_regtest"),
                ),
            ),
            max_block=_optional_non_negative_int(data, "max_block", "blocksci"),
        )

    def append_arguments(self, arguments: list[str]) -> None:
        _append_option(arguments, "--blocksci-workflow", self.workflow)
        _append_option(arguments, "--blocksci-task", self.task)
        _append_option(arguments, "--blocksci-script", self.script)
        _append_option(
            arguments,
            "--blocksci-cache-source-run-id",
            self.cache_source_run_id,
        )
        _append_option(arguments, "--blocksci-notebook-port", self.notebook_port)
        _append_option(arguments, "--blocksci-notebooks-dir", self.notebooks_dir)
        _append_option(
            arguments,
            "--blocksci-external-bitcoin-datadir",
            self.external_bitcoin_datadir,
        )
        _append_option(
            arguments,
            "--blocksci-external-blocksci-dir",
            self.external_blocksci_dir,
        )
        _append_option(arguments, "--blocksci-network", self.network)
        _append_option(arguments, "--blocksci-max-block", self.max_block)


@dataclass(frozen=True, slots=True)
class JoinMarketConfiguration:
    detector: JoinMarketDetector | None = None
    min_base_fee: int | None = None
    percentage_fee: float | None = None
    max_depth: int | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> JoinMarketConfiguration:
        data = _mapping(value, "joinmarket")
        _reject_unknown(
            data,
            {"detector", "min_base_fee", "percentage_fee", "max_depth"},
            "joinmarket",
        )
        return cls(
            detector=cast(
                JoinMarketDetector | None,
                _choice(
                    data, "detector", "joinmarket", ("possible", "definite")
                ),
            ),
            min_base_fee=_optional_non_negative_int(
                data, "min_base_fee", "joinmarket"
            ),
            percentage_fee=_optional_non_negative_float(
                data, "percentage_fee", "joinmarket"
            ),
            max_depth=_optional_positive_int(data, "max_depth", "joinmarket"),
        )

    def append_arguments(self, arguments: list[str]) -> None:
        _append_option(arguments, "--joinmarket-detector", self.detector)
        _append_option(arguments, "--joinmarket-min-base-fee", self.min_base_fee)
        _append_option(
            arguments, "--joinmarket-percentage-fee", self.percentage_fee
        )
        _append_option(arguments, "--joinmarket-max-depth", self.max_depth)


@dataclass(frozen=True, slots=True)
class MappingsConfiguration:
    mining_fee_rate: int | None = None
    coordination_fee_rate: float | None = None
    max_decomposition_fee: int | None = None
    mode: MappingMode | None = None
    timeout: int | None = None
    retry_timeout: int | None = None
    sake_seed: int | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> MappingsConfiguration:
        data = _mapping(value, "mappings")
        _reject_unknown(
            data,
            {
                "mining_fee_rate",
                "coordination_fee_rate",
                "max_decomposition_fee",
                "mode",
                "timeout",
                "retry_timeout",
                "sake_seed",
            },
            "mappings",
        )
        return cls(
            mining_fee_rate=_optional_non_negative_int(
                data, "mining_fee_rate", "mappings"
            ),
            coordination_fee_rate=_optional_non_negative_float(
                data, "coordination_fee_rate", "mappings"
            ),
            max_decomposition_fee=_optional_non_negative_int(
                data, "max_decomposition_fee", "mappings"
            ),
            mode=cast(
                MappingMode | None,
                _choice(data, "mode", "mappings", ("numeric", "all")),
            ),
            timeout=_optional_positive_int(data, "timeout", "mappings"),
            retry_timeout=_optional_positive_int(
                data, "retry_timeout", "mappings"
            ),
            sake_seed=_optional_non_negative_int(data, "sake_seed", "mappings"),
        )

    def append_arguments(self, arguments: list[str]) -> None:
        _append_option(arguments, "--mapping-mining-fee-rate", self.mining_fee_rate)
        _append_option(
            arguments,
            "--mapping-coordination-fee-rate",
            self.coordination_fee_rate,
        )
        _append_option(
            arguments,
            "--mapping-max-decomposition-fee",
            self.max_decomposition_fee,
        )
        _append_option(arguments, "--mapping-mode", self.mode)
        _append_option(arguments, "--mapping-timeout", self.timeout)
        _append_option(arguments, "--mapping-retry-timeout", self.retry_timeout)
        _append_option(arguments, "--sake-seed", self.sake_seed)


@dataclass(frozen=True, slots=True)
class ExternalAnalysisConfiguration:
    bitcoin_datadir: str | None = None
    baseline: str | None = None
    false_cjtxs: tuple[str, ...] = ()
    network: ExternalNetwork | None = None
    min_free_gb: int | None = None
    resume: bool = False

    @classmethod
    def from_mapping(cls, value: Any) -> ExternalAnalysisConfiguration:
        data = _mapping(value, "external")
        _reject_unknown(
            data,
            {
                "bitcoin_datadir",
                "baseline",
                "false_cjtxs",
                "network",
                "min_free_gb",
                "resume",
            },
            "external",
        )
        return cls(
            bitcoin_datadir=_optional_string(
                data, "bitcoin_datadir", "external"
            ),
            baseline=_optional_string(data, "baseline", "external"),
            false_cjtxs=_string_tuple(data, "false_cjtxs", "external"),
            network=cast(
                ExternalNetwork | None,
                _choice(data, "network", "external", ("bitcoin",)),
            ),
            min_free_gb=_optional_non_negative_int(
                data, "min_free_gb", "external"
            ),
            resume=_boolean(data, "resume", "external"),
        )

    @property
    def configured(self) -> bool:
        return any(
            (
                self.bitcoin_datadir is not None,
                self.baseline is not None,
                bool(self.false_cjtxs),
                self.network is not None,
                self.min_free_gb is not None,
                self.resume,
            )
        )

    def append_arguments(self, arguments: list[str]) -> None:
        _append_option(arguments, "--bitcoin-datadir", self.bitcoin_datadir)
        _append_option(arguments, "--baseline", self.baseline)
        for path in self.false_cjtxs:
            _append_option(arguments, "--false-cjtxs", path)
        _append_option(arguments, "--network", self.network)
        _append_option(arguments, "--min-free-gb", self.min_free_gb)
        _append_flag(arguments, "--resume", self.resume)


@dataclass(frozen=True, slots=True)
class StageConfiguration:
    analysis: bool = False
    blocksci: bool = False
    mappings: bool = False

    @classmethod
    def from_mapping(cls, value: Any) -> StageConfiguration:
        data = _mapping(value, "stages")
        _reject_unknown(data, {"analysis", "blocksci", "mappings"}, "stages")
        return cls(
            analysis=_boolean(data, "analysis", "stages"),
            blocksci=_boolean(data, "blocksci", "stages"),
            mappings=_boolean(data, "mappings", "stages"),
        )


@dataclass(frozen=True, slots=True)
class PBSResourceConfiguration:
    ncpus: int | None = None
    mem: str | None = None
    scratch: str | None = None
    walltime: str | None = None

    @classmethod
    def from_mapping(cls, value: Any, location: str) -> PBSResourceConfiguration:
        data = _mapping(value, location)
        _reject_unknown(data, set(PBS_RESOURCES), location)
        return cls(
            ncpus=_optional_positive_int(data, "ncpus", location),
            mem=_optional_string(data, "mem", location),
            scratch=_optional_string(data, "scratch", location),
            walltime=_optional_string(data, "walltime", location),
        )

    @property
    def configured(self) -> bool:
        return any(
            value is not None
            for value in (self.ncpus, self.mem, self.scratch, self.walltime)
        )

    def append_arguments(self, arguments: list[str], prefix: str) -> None:
        _append_option(arguments, f"{prefix}-ncpus", self.ncpus)
        _append_option(arguments, f"{prefix}-mem", self.mem)
        _append_option(arguments, f"{prefix}-scratch", self.scratch)
        _append_option(arguments, f"{prefix}-walltime", self.walltime)


@dataclass(frozen=True, slots=True)
class PBSConfiguration:
    ncpus: int | None = None
    mem: str | None = None
    scratch: str | None = None
    walltime: str | None = None
    image: str | None = None
    blocksci_image: str | None = None
    coinjoin_analysis_image: str | None = None
    mappings_enumerator_image: str | None = None
    sake_image: str | None = None
    bitcoin_datadir: str | None = None
    analysis: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    blocksci: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    mappings: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    unified_report: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)

    @classmethod
    def from_mapping(cls, value: Any) -> PBSConfiguration:
        data = _mapping(value, "pbs")
        _reject_unknown(
            data,
            {
                *PBS_RESOURCES,
                "image",
                "blocksci_image",
                "coinjoin_analysis_image",
                "mappings_enumerator_image",
                "sake_image",
                "bitcoin_datadir",
                "analysis",
                "blocksci",
                "mappings",
                "unified_report",
            },
            "pbs",
        )
        shared = PBSResourceConfiguration.from_mapping(
            {resource: data[resource] for resource in PBS_RESOURCES if resource in data},
            "pbs",
        )
        return cls(
            ncpus=shared.ncpus,
            mem=shared.mem,
            scratch=shared.scratch,
            walltime=shared.walltime,
            image=_optional_string(data, "image", "pbs"),
            blocksci_image=_optional_string(data, "blocksci_image", "pbs"),
            coinjoin_analysis_image=_optional_string(
                data, "coinjoin_analysis_image", "pbs"
            ),
            mappings_enumerator_image=_optional_string(
                data, "mappings_enumerator_image", "pbs"
            ),
            sake_image=_optional_string(data, "sake_image", "pbs"),
            bitcoin_datadir=_optional_string(data, "bitcoin_datadir", "pbs"),
            analysis=PBSResourceConfiguration.from_mapping(data.get("analysis"), "pbs.analysis"),
            blocksci=PBSResourceConfiguration.from_mapping(data.get("blocksci"), "pbs.blocksci"),
            mappings=PBSResourceConfiguration.from_mapping(data.get("mappings"), "pbs.mappings"),
            unified_report=PBSResourceConfiguration.from_mapping(
                data.get("unified_report"), "pbs.unified_report"
            ),
        )

    def append_shared_arguments(self, arguments: list[str]) -> None:
        _append_option(arguments, "--pbs-ncpus", self.ncpus)
        _append_option(arguments, "--pbs-mem", self.mem)
        _append_option(arguments, "--pbs-scratch", self.scratch)
        _append_option(arguments, "--pbs-walltime", self.walltime)
        _append_option(arguments, "--pbs-image", self.image)
        _append_option(arguments, "--pbs-blocksci-image", self.blocksci_image)
        _append_option(
            arguments,
            "--pbs-coinjoin-analysis-image",
            self.coinjoin_analysis_image,
        )
        _append_option(
            arguments,
            "--pbs-mappings-enumerator-image",
            self.mappings_enumerator_image,
        )
        _append_option(arguments, "--pbs-sake-image", self.sake_image)
        _append_option(arguments, "--pbs-bitcoin-datadir", self.bitcoin_datadir)


@dataclass(frozen=True, slots=True)
class PipelineConfiguration:
    action: PipelineAction = "full-run"
    runtime: ContainerRuntime | None = None
    runs_root: str | None = None
    engine: Engine | None = None
    coinjoin_type: CoinjoinType | None = None
    driver: Driver | None = None
    scenario: str | None = None
    run_dir: str | None = None
    run_id: str | None = None
    run_timezone: str | None = None
    min_input_count: int | None = None
    test_values: bool = False
    dry_run: bool = False
    parallel: bool = False
    all_runs: bool = False
    yes: bool = False
    analysis_action: AnalysisAction | None = None
    emulation_timeout: int | None = None
    kubernetes: KubernetesConfiguration = field(default_factory=KubernetesConfiguration)
    artifacts: ArtifactConfiguration = field(default_factory=ArtifactConfiguration)
    images: ImageConfiguration = field(default_factory=ImageConfiguration)
    blocksci: BlockSciConfiguration = field(default_factory=BlockSciConfiguration)
    joinmarket: JoinMarketConfiguration = field(default_factory=JoinMarketConfiguration)
    mappings: MappingsConfiguration = field(default_factory=MappingsConfiguration)
    external: ExternalAnalysisConfiguration = field(
        default_factory=ExternalAnalysisConfiguration
    )
    stages: StageConfiguration = field(default_factory=StageConfiguration)
    pbs: PBSConfiguration = field(default_factory=PBSConfiguration)

    @classmethod
    def from_mapping(cls, value: Any) -> PipelineConfiguration:
        data = _mapping(value, "configuration")
        _reject_unknown(
            data,
            {
                "action",
                "runtime",
                "runs_root",
                "engine",
                "coinjoin_type",
                "driver",
                "scenario",
                "run_dir",
                "run_id",
                "run_timezone",
                "min_input_count",
                "test_values",
                "dry_run",
                "parallel",
                "all_runs",
                "yes",
                "analysis_action",
                "emulation_timeout",
                "kubernetes",
                "artifacts",
                "images",
                "blocksci",
                "joinmarket",
                "mappings",
                "external",
                "stages",
                "pbs",
            },
            "top-level",
        )
        raw_action = _choice(
            data,
            "action",
            "configuration",
            tuple(sorted(KNOWN_ACTIONS | set(YAML_ACTION_ALIASES))),
            default="full-run",
        )
        action = cast(
            PipelineAction,
            YAML_ACTION_ALIASES.get(cast(str, raw_action), cast(str, raw_action)),
        )
        configuration = cls(
            action=action,
            runtime=cast(
                ContainerRuntime | None,
                _choice(data, "runtime", "configuration", ("docker", "podman")),
            ),
            runs_root=_optional_string(data, "runs_root", "configuration"),
            engine=cast(
                Engine | None,
                _choice(data, "engine", "configuration", ("wasabi", "joinmarket")),
            ),
            coinjoin_type=cast(
                CoinjoinType | None,
                _choice(
                    data,
                    "coinjoin_type",
                    "configuration",
                    ("wasabi2", "joinmarket"),
                ),
            ),
            driver=cast(
                Driver | None,
                _choice(data, "driver", "configuration", ("docker", "kubernetes")),
            ),
            scenario=_optional_string(data, "scenario", "configuration"),
            run_dir=_optional_string(data, "run_dir", "configuration"),
            run_id=_optional_string(data, "run_id", "configuration"),
            run_timezone=_optional_string(data, "run_timezone", "configuration"),
            min_input_count=_optional_positive_int(data, "min_input_count", "configuration"),
            test_values=_boolean(data, "test_values", "configuration"),
            dry_run=_boolean(data, "dry_run", "configuration"),
            parallel=_boolean(data, "parallel", "configuration"),
            all_runs=_boolean(data, "all_runs", "configuration"),
            yes=_boolean(data, "yes", "configuration"),
            analysis_action=cast(
                AnalysisAction | None,
                _choice(
                    data,
                    "analysis_action",
                    "configuration",
                    ("collect_docker", "analyze_only"),
                ),
            ),
            emulation_timeout=_optional_positive_int(
                data, "emulation_timeout", "configuration"
            ),
            kubernetes=KubernetesConfiguration.from_mapping(data.get("kubernetes")),
            artifacts=ArtifactConfiguration.from_mapping(data.get("artifacts")),
            images=ImageConfiguration.from_mapping(data.get("images")),
            blocksci=BlockSciConfiguration.from_mapping(data.get("blocksci")),
            joinmarket=JoinMarketConfiguration.from_mapping(data.get("joinmarket")),
            mappings=MappingsConfiguration.from_mapping(data.get("mappings")),
            external=ExternalAnalysisConfiguration.from_mapping(data.get("external")),
            stages=StageConfiguration.from_mapping(data.get("stages")),
            pbs=PBSConfiguration.from_mapping(data.get("pbs")),
        )
        configuration.validate()
        return configuration

    def validate(self) -> None:
        if self.action != "external analyze":
            if self.external.configured:
                raise ConfigurationError(
                    "external settings require action: external analyze"
                )
            return
        supplied_inputs = (
            self.external.bitcoin_datadir is not None
            or self.external.baseline is not None
        )
        if self.external.resume and supplied_inputs:
            raise ConfigurationError(
                "external.resume cannot be combined with external.bitcoin_datadir "
                "or external.baseline"
            )
        if not self.external.resume and (
            self.external.bitcoin_datadir is None
            or self.external.baseline is None
        ):
            raise ConfigurationError(
                "a new external analysis requires external.bitcoin_datadir and "
                "external.baseline"
            )

    @classmethod
    def from_yaml(cls, path: Path) -> PipelineConfiguration:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ConfigurationError(f"cannot read configuration {path}: {error}") from error
        except yaml.YAMLError as error:
            raise ConfigurationError(f"invalid YAML in {path}: {error}") from error
        return cls.from_mapping(loaded)

    def to_arguments(self) -> list[str]:
        arguments = self.action.split()
        _append_option(arguments, "--runtime", self.runtime)
        _append_option(arguments, "--runs-root", self.runs_root)
        self.images.append_arguments(arguments)
        _append_option(arguments, "--engine", self.engine)
        _append_option(arguments, "--coinjoin-type", self.coinjoin_type)
        _append_option(arguments, "--driver", self.driver)
        _append_option(arguments, "--scenario", self.scenario)
        _append_option(arguments, "--run-dir", self.run_dir)
        _append_option(arguments, "--run-id", self.run_id)
        _append_option(arguments, "--run-timezone", self.run_timezone)
        _append_option(arguments, "--min-input-count", self.min_input_count)
        _append_flag(arguments, "--test-values", self.test_values)
        _append_flag(arguments, "--dry-run", self.dry_run)
        _append_flag(arguments, "--parallel", self.parallel)
        _append_flag(arguments, "--all-runs", self.all_runs)
        _append_flag(arguments, "--yes", self.yes)
        _append_option(arguments, "--analysis-action", self.analysis_action)
        _append_option(arguments, "--emulation-timeout", self.emulation_timeout)
        self.kubernetes.append_arguments(arguments)
        self.artifacts.append_arguments(arguments)
        self.blocksci.append_arguments(arguments)
        self.joinmarket.append_arguments(arguments)
        self.mappings.append_arguments(arguments)
        self.external.append_arguments(arguments)
        self.pbs.append_shared_arguments(arguments)

        enabled_stages = {
            stage
            for stage in ("analysis", "blocksci", "mappings")
            if getattr(self.stages, stage)
        }
        stage_flags = {
            "analysis": "--analysisPbs",
            "blocksci": "--blocksciPbs",
            "mappings": "--mappingsPbs",
        }
        for stage, flag in stage_flags.items():
            resources = cast(PBSResourceConfiguration, getattr(self.pbs, stage))
            resources.append_arguments(arguments, f"--pbs-{stage}")
            if resources.configured:
                enabled_stages.add(stage)
            if stage in enabled_stages:
                arguments.append(flag)
        self.pbs.unified_report.append_arguments(arguments, "--pbs-unified-report")
        return arguments


def configuration_arguments(path: Path) -> list[str]:
    """Load *path* into a typed model and return equivalent CLI arguments."""
    return PipelineConfiguration.from_yaml(path).to_arguments()


def expand_configuration(argv: list[str]) -> tuple[list[str], Path | None]:
    """Replace one --from-configuration option with its flattened YAML arguments."""
    aliases = ("--from-configuration", "--fromConfiguration")
    matches: list[tuple[int, str | None, bool]] = []
    for index, item in enumerate(argv):
        if item in aliases:
            value = argv[index + 1] if index + 1 < len(argv) else None
            matches.append((index, value, True))
        else:
            for alias in aliases:
                if item.startswith(f"{alias}="):
                    matches.append((index, item.split("=", 1)[1], False))
    if not matches:
        return argv, None
    if len(matches) != 1:
        raise ConfigurationError("specify --from-configuration only once")
    index, value, consumes_next = matches[0]
    if not value or value.startswith("--"):
        raise ConfigurationError("--from-configuration requires a YAML file path")
    remaining = list(argv)
    del remaining[index : index + (2 if consumes_next else 1)]
    contains_external_action = any(
        remaining[index : index + 2] == ["external", "analyze"]
        for index in range(len(remaining) - 1)
    )
    if any(item in KNOWN_ACTIONS for item in remaining) or contains_external_action:
        raise ConfigurationError(
            "put the action in YAML when using --from-configuration"
        )
    path = Path(value).expanduser().resolve()
    configured = PipelineConfiguration.from_yaml(path).to_arguments()

    # Explicit CLI flags win over YAML values. This also avoids duplicate
    # argparse values while still allowing host options such as --version.
    explicit_flags = {
        item.split("=", 1)[0] for item in remaining if item.startswith("--")
    }
    merged = [configured[0]]
    position = 1
    while position < len(configured):
        item = configured[position]
        if item in explicit_flags:
            position += 1
            if position < len(configured) and not configured[position].startswith("--"):
                position += 1
            continue
        merged.append(item)
        position += 1
        if position < len(configured) and not configured[position].startswith("--"):
            merged.append(configured[position])
            position += 1
    return [*merged, *remaining], path
