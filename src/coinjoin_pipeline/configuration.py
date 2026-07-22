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
]
Engine = Literal["wasabi", "joinmarket"]
Driver = Literal["docker", "kubernetes"]
CoinjoinType = Literal["wasabi2", "joinmarket"]
ArtifactBackend = Literal["shared-storage", "s3"]

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
}
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


def _optional_positive_int(
    mapping: Mapping[str, Any], key: str, location: str
) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigurationError(f"{location}.{key} must be a positive integer")
    return value


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


def _append_option(arguments: list[str], flag: str, value: str | int | None) -> None:
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
    copy_to_host: bool = False

    @classmethod
    def from_mapping(cls, value: Any) -> KubernetesConfiguration:
        data = _mapping(value, "kubernetes")
        _reject_unknown(
            data,
            {"namespace", "reuse_namespace", "kubeconfig", "image_prefix", "copy_to_host"},
            "kubernetes",
        )
        return cls(
            namespace=_optional_string(data, "namespace", "kubernetes"),
            reuse_namespace=_boolean(data, "reuse_namespace", "kubernetes"),
            kubeconfig=_optional_string(data, "kubeconfig", "kubernetes"),
            image_prefix=_optional_string(data, "image_prefix", "kubernetes"),
            copy_to_host=_boolean(data, "copy_to_host", "kubernetes"),
        )

    def append_arguments(self, arguments: list[str]) -> None:
        _append_option(arguments, "--namespace", self.namespace)
        _append_flag(arguments, "--reuse-namespace", self.reuse_namespace)
        _append_option(arguments, "--kubeconfig", self.kubeconfig)
        _append_option(arguments, "--image-prefix", self.image_prefix)
        _append_flag(arguments, "--copy-to-host", self.copy_to_host)


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
    analysis: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    blocksci: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    mappings: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)
    unified_report: PBSResourceConfiguration = field(default_factory=PBSResourceConfiguration)

    @classmethod
    def from_mapping(cls, value: Any) -> PBSConfiguration:
        data = _mapping(value, "pbs")
        _reject_unknown(data, {*PBS_RESOURCES, "analysis", "blocksci", "mappings", "unified_report"}, "pbs")
        shared = PBSResourceConfiguration.from_mapping(
            {resource: data[resource] for resource in PBS_RESOURCES if resource in data},
            "pbs",
        )
        return cls(
            ncpus=shared.ncpus,
            mem=shared.mem,
            scratch=shared.scratch,
            walltime=shared.walltime,
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


@dataclass(frozen=True, slots=True)
class PipelineConfiguration:
    action: PipelineAction = "full-run"
    engine: Engine | None = None
    coinjoin_type: CoinjoinType | None = None
    driver: Driver | None = None
    scenario: str | None = None
    run_id: str | None = None
    run_timezone: str | None = None
    min_input_count: int | None = None
    test_values: bool = False
    dry_run: bool = False
    parallel: bool = False
    kubernetes: KubernetesConfiguration = field(default_factory=KubernetesConfiguration)
    artifacts: ArtifactConfiguration = field(default_factory=ArtifactConfiguration)
    stages: StageConfiguration = field(default_factory=StageConfiguration)
    pbs: PBSConfiguration = field(default_factory=PBSConfiguration)

    @classmethod
    def from_mapping(cls, value: Any) -> PipelineConfiguration:
        data = _mapping(value, "configuration")
        _reject_unknown(
            data,
            {
                "action",
                "engine",
                "coinjoin_type",
                "driver",
                "scenario",
                "run_id",
                "run_timezone",
                "min_input_count",
                "test_values",
                "dry_run",
                "parallel",
                "kubernetes",
                "artifacts",
                "stages",
                "pbs",
            },
            "top-level",
        )
        return cls(
            action=cast(
                PipelineAction,
                _choice(
                    data,
                    "action",
                    "configuration",
                    tuple(sorted(KNOWN_ACTIONS)),
                    default="full-run",
                ),
            ),
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
            run_id=_optional_string(data, "run_id", "configuration"),
            run_timezone=_optional_string(data, "run_timezone", "configuration"),
            min_input_count=_optional_positive_int(data, "min_input_count", "configuration"),
            test_values=_boolean(data, "test_values", "configuration"),
            dry_run=_boolean(data, "dry_run", "configuration"),
            parallel=_boolean(data, "parallel", "configuration"),
            kubernetes=KubernetesConfiguration.from_mapping(data.get("kubernetes")),
            artifacts=ArtifactConfiguration.from_mapping(data.get("artifacts")),
            stages=StageConfiguration.from_mapping(data.get("stages")),
            pbs=PBSConfiguration.from_mapping(data.get("pbs")),
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
        arguments: list[str] = [self.action]
        _append_option(arguments, "--engine", self.engine)
        _append_option(arguments, "--coinjoin-type", self.coinjoin_type)
        _append_option(arguments, "--driver", self.driver)
        _append_option(arguments, "--scenario", self.scenario)
        _append_option(arguments, "--run-id", self.run_id)
        _append_option(arguments, "--run-timezone", self.run_timezone)
        _append_option(arguments, "--min-input-count", self.min_input_count)
        _append_flag(arguments, "--test-values", self.test_values)
        _append_flag(arguments, "--dry-run", self.dry_run)
        _append_flag(arguments, "--parallel", self.parallel)
        self.kubernetes.append_arguments(arguments)
        self.artifacts.append_arguments(arguments)
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
    if any(item in KNOWN_ACTIONS for item in remaining):
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
