"""Cross-field policy shared by YAML and CLI configuration inputs."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

from .configuration import cli_flag, config_value, option_specs
from .storage.s3 import RUN_ID_RULE, is_valid_run_id

if TYPE_CHECKING:
    from .configuration import PipelineConfiguration, PBSResourceConfiguration


def _supplied(config: PipelineConfiguration, path: str, value: object) -> bool:
    """Explicit false/null is inactive; zero and empty strings remain supplied."""
    return path in config.provided and value is not None and value is not False and value != ()


def _resources_supplied(config: PipelineConfiguration, stage: str, resources: PBSResourceConfiguration) -> bool:
    return any(
        _supplied(config, f"pbs.{stage}.{item.name}", getattr(resources, item.name)) for item in fields(resources)
    )


def cross_option_errors(config: PipelineConfiguration) -> list[str]:
    """Return every violated rule in order, retaining CLI spellings in diagnostics."""
    return _action_errors(config) + _general_errors(config)


def _general_errors(config: PipelineConfiguration) -> list[str]:
    """Rules that hold for every action."""
    errors: list[str] = []
    for path, destination, item in option_specs():
        permitted = item.metadata.get("actions")
        if permitted and path in config.provided and config.action not in permitted:
            value = config_value(config, path)
            if value is not None and value is not False:
                errors.append(f"{cli_flag(destination)} is not supported for {config.action}")
    if config.artifacts.backend == "s3" and config.action != "pbs-from-s3":
        for flag, value in (
            ("--kubernetes-btc-datadir", config.kubernetes.btc_datadir),
            ("--copy-to-host", config.kubernetes.copy_to_host),
        ):
            if value:
                errors.append(f"Kubernetes S3-compatible mode does not support shared Bitcoin storage ({flag})")
    return errors


@dataclass(frozen=True)
class _PbsStages:
    """The PBS stages a configuration requests explicitly."""

    analysis: bool
    blocksci: bool

    @classmethod
    def requested(cls, config: PipelineConfiguration) -> _PbsStages:
        stages = config.stages
        return cls(
            analysis=_supplied(config, "stages.analysis", stages.analysis),
            blocksci=_supplied(config, "stages.blocksci", stages.blocksci),
        )


def _action_errors(config: PipelineConfiguration) -> list[str]:
    """Rules of the emulation and analysis actions."""
    stages = _PbsStages.requested(config)
    return [
        *_run_selection_errors(config),
        *_driver_errors(config),
        *_pbs_stage_errors(config, stages),
        *_s3_errors(config, stages),
        *_remaining_errors(config, stages),
    ]


def _run_selection_errors(config: PipelineConfiguration) -> list[str]:
    errors: list[str] = []
    action = config.action
    if config.run_dir is not None and config.all_runs:
        errors.append("--run-dir and --all-runs are mutually exclusive")
    if action in {"analyze", "export", "coinjoin-analysis"}:
        if not _supplied(config, "run_dir", config.run_dir) and not _supplied(config, "all_runs", config.all_runs):
            errors.append(f"{action} requires --run-dir (or --all-runs where supported)")
    if (
        action == "clean"
        and not _supplied(config, "dry_run", config.dry_run)
        and not _supplied(config, "yes", config.yes)
    ):
        errors.append("clean is destructive; pass --yes or --dry-run")
    if config.run_id is not None and not is_valid_run_id(config.run_id):
        errors.append(f"--run-id {RUN_ID_RULE}")
    return errors


def _driver_errors(config: PipelineConfiguration) -> list[str]:
    if config.driver == "kubernetes":
        return []
    kubernetes = config.kubernetes
    return [
        f"{flag} requires --driver kubernetes"
        for flag, path, value in (
            ("--kubeconfig", "kubernetes.kubeconfig", kubernetes.kubeconfig),
            ("--namespace", "kubernetes.namespace", kubernetes.namespace),
            ("--reuse-namespace", "kubernetes.reuse_namespace", kubernetes.reuse_namespace),
            ("--copy-to-host", "kubernetes.copy_to_host", kubernetes.copy_to_host),
        )
        if _supplied(config, path, value)
    ]


def _pbs_stage_errors(config: PipelineConfiguration, stages: _PbsStages) -> list[str]:
    errors: list[str] = []
    for flag, enabled, permitted in (
        ("--analysis-pbs", stages.analysis, ("pbs-from-s3",)),
        ("--blocksci-pbs", stages.blocksci, ("pbs-from-s3",)),
    ):
        if enabled and config.action not in permitted:
            errors.append(f"{flag} is supported only by {', '.join(permitted)}")
    for stage, resources, enabled, flag in (
        ("analysis", config.pbs.analysis, stages.analysis, "--analysis-pbs"),
        ("blocksci", config.pbs.blocksci, stages.blocksci, "--blocksci-pbs"),
    ):
        if _resources_supplied(config, stage, resources) and not enabled:
            errors.append(f"{stage}-specific PBS resources require {flag}")
    return errors


def _missing_s3_options(config: PipelineConfiguration, *, with_secret: bool) -> list[str]:
    """CLI flags of the S3 settings an S3 run lacks, in their documented order."""
    artifacts = config.artifacts
    required = (
        ("--run-id", config.run_id),
        ("--artifact-uri", artifacts.uri),
        ("--s3-endpoint-url", artifacts.endpoint_url),
        *((("--s3-secret-name", artifacts.secret_name),) if with_secret else ()),
        ("--s3-credentials-file", artifacts.credentials_file),
        ("--s3-profile", artifacts.profile),
    )
    return [flag for flag, value in required if not value]


def _s3_errors(config: PipelineConfiguration, stages: _PbsStages) -> list[str]:
    errors: list[str] = []
    action = config.action
    s3_backend = config.artifacts.backend == "s3"
    if action == "pbs-from-s3":
        errors.extend(f"pbs-from-s3 requires {flag}" for flag in _missing_s3_options(config, with_secret=False))
        if not any((stages.analysis, stages.blocksci)):
            errors.append("pbs-from-s3 requires --analysis-pbs or --blocksci-pbs")
    if (
        s3_backend
        and action == "pbs-from-s3"
        and _resources_supplied(config, "unified_report", config.pbs.unified_report)
        and not stages.blocksci
    ):
        errors.append(
            "unified-report PBS resource overrides require a unified-report job: "
            "BlockSci detection with a coinjoin-analysis baseline"
        )
    if s3_backend and action == "emulate":
        errors.extend(
            f"Kubernetes S3-compatible mode requires {flag}" for flag in _missing_s3_options(config, with_secret=True)
        )
        if config.driver != "kubernetes":
            errors.append("--artifact-backend s3 requires --driver kubernetes")
        if not _supplied(config, "kubernetes.reuse_namespace", config.kubernetes.reuse_namespace):
            errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created"
            )
    return errors


def _remaining_errors(config: PipelineConfiguration, stages: _PbsStages) -> list[str]:
    errors: list[str] = []
    blocksci = config.blocksci
    if blocksci.script and not Path(blocksci.script).expanduser().is_file():
        errors.append(f"BlockSci script not found: {blocksci.script}")
    return errors
