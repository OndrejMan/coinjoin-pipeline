"""Cross-field policy shared by YAML and CLI configuration inputs."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

from .configuration import cli_flag, config_value, option_specs
from .execution.stages import runs_unified_report
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
    if not 1024 <= config.blocksci.notebook_port <= 65535:
        errors.append("--blocksci-notebook-port must be between 1024 and 65535")
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
    mappings: bool

    @classmethod
    def requested(cls, config: PipelineConfiguration) -> _PbsStages:
        stages = config.stages
        return cls(
            analysis=_supplied(config, "stages.analysis", stages.analysis),
            blocksci=_supplied(config, "stages.blocksci", stages.blocksci),
            mappings=_supplied(config, "stages.mappings", stages.mappings),
        )

    @property
    def blocksci_alone(self) -> bool:
        return self.blocksci and not self.analysis


def _action_errors(config: PipelineConfiguration) -> list[str]:
    """Rules of the emulation and analysis actions."""
    stages = _PbsStages.requested(config)
    return [
        *_run_selection_errors(config),
        *_driver_errors(config),
        *_pbs_stage_errors(config, stages),
        *_blocksci_task_errors(config, stages),
        *_height_errors(config),
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
        ("--analysis-pbs", stages.analysis, ("full-run", "pbs-from-s3")),
        ("--blocksci-pbs", stages.blocksci, ("full-run", "pbs-from-s3")),
        ("--mappings-pbs", stages.mappings, ("full-run", "pbs-from-s3")),
    ):
        if enabled and config.action not in permitted:
            errors.append(f"{flag} is supported only by {', '.join(permitted)}")
    for stage, resources, enabled, flag in (
        ("analysis", config.pbs.analysis, stages.analysis, "--analysis-pbs"),
        ("blocksci", config.pbs.blocksci, stages.blocksci, "--blocksci-pbs"),
        ("mappings", config.pbs.mappings, stages.mappings, "--mappings-pbs"),
    ):
        if _resources_supplied(config, stage, resources) and not enabled:
            errors.append(f"{stage}-specific PBS resources require {flag}")
    return errors


def _blocksci_task_errors(config: PipelineConfiguration, stages: _PbsStages) -> list[str]:
    """Which action, workflow and PBS stages each BlockSci task runs with."""
    errors: list[str] = []
    action = config.action
    blocksci = config.blocksci
    workflow, task = blocksci.workflow, blocksci.task
    if workflow != "combined" and not stages.blocksci:
        errors.append("reusable BlockSci workflows require --blocksci-pbs")
    if task == "parse":
        if action != "pbs-from-s3" or workflow != "reusable":
            errors.append("--blocksci-task parse requires pbs-from-s3 --blocksci-workflow reusable")
        if not stages.blocksci_alone:
            errors.append("--blocksci-task parse requires --blocksci-pbs without --analysis-pbs")
    elif task != "detect":
        if action != "pbs-from-s3":
            errors.append("BlockSci reusable tasks are submitted with pbs-from-s3")
        if workflow == "combined":
            errors.append("BlockSci reusable tasks require --blocksci-workflow reusable or cached")
        if not stages.blocksci_alone:
            errors.append("BlockSci reusable tasks require --blocksci-pbs without --analysis-pbs")
    if action == "pbs-from-s3" and task == "script" and not blocksci.script:
        errors.append("--blocksci-task script requires --blocksci-script")
    if action == "pbs-from-s3" and task != "script" and _supplied(config, "blocksci.script", blocksci.script):
        errors.append("--blocksci-script requires --blocksci-task script")
    if task != "notebook":
        for flag, path, value in (
            ("--blocksci-notebooks-dir", "blocksci.notebooks_dir", blocksci.notebooks_dir),
            ("--blocksci-notebook-port", "blocksci.notebook_port", blocksci.notebook_port),
        ):
            if _supplied(config, path, value):
                errors.append(f"{flag} requires --blocksci-task notebook")
    return errors


def _height_errors(config: PipelineConfiguration) -> list[str]:
    errors: list[str] = []
    blocksci = config.blocksci
    pin_height, scan_start, scan_end = blocksci.pin_height, blocksci.scan_start_height, blocksci.scan_end_height
    if blocksci.task != "script":
        for flag, path, height in (
            ("--blocksci-pin-height", "blocksci.pin_height", pin_height),
            ("--blocksci-scan-start-height", "blocksci.scan_start_height", scan_start),
            ("--blocksci-scan-end-height", "blocksci.scan_end_height", scan_end),
        ):
            if _supplied(config, path, height):
                errors.append(f"{flag} requires --blocksci-task script")
    if scan_start is not None and scan_end is not None and scan_start >= scan_end:
        errors.append("--blocksci-scan-start-height must be below --blocksci-scan-end-height")
    if pin_height is not None and scan_end is not None and scan_end > pin_height + 1:
        errors.append("--blocksci-scan-end-height must not exceed --blocksci-pin-height + 1")
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
        if not any((stages.analysis, stages.blocksci, stages.mappings)):
            errors.append("pbs-from-s3 requires --analysis-pbs, --blocksci-pbs, or --mappings-pbs")
    if (
        s3_backend
        and action in ("full-run", "pbs-from-s3")
        and _resources_supplied(config, "unified_report", config.pbs.unified_report)
        and not runs_unified_report(
            action=action,
            analysis_pbs=stages.analysis,
            blocksci_pbs=stages.blocksci,
            blocksci_task=config.blocksci.task,
        )
    ):
        errors.append(
            "unified-report PBS resource overrides require a unified-report job: "
            "BlockSci detection with a coinjoin-analysis baseline"
        )
    if s3_backend and action in ("full-run", "emulate"):
        mode = "full-run --artifact-backend s3" if action == "full-run" else "Kubernetes S3-compatible mode"
        errors.extend(f"{mode} requires {flag}" for flag in _missing_s3_options(config, with_secret=True))
        if config.driver != "kubernetes":
            errors.append(
                f"{'full-run ' if action == 'full-run' else ''}--artifact-backend s3 requires --driver kubernetes"
            )
        if not _supplied(config, "kubernetes.reuse_namespace", config.kubernetes.reuse_namespace):
            errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created"
            )
    s3_emulation = s3_backend and action in ("full-run", "emulate") and config.driver == "kubernetes"
    for flag, path, value in (
        (
            "--distributor-startup-timeout",
            "kubernetes.distributor_startup_timeout",
            config.kubernetes.distributor_startup_timeout,
        ),
        ("--btc-node-image", "kubernetes.btc_node_image", config.kubernetes.btc_node_image),
        (
            "--btc-node-initial-block-count",
            "kubernetes.btc_node_initial_block_count",
            config.kubernetes.btc_node_initial_block_count,
        ),
        ("--kubernetes-image-pull-policy", "kubernetes.image_pull_policy", config.kubernetes.image_pull_policy),
    ):
        if _supplied(config, path, value) and not s3_emulation:
            errors.append(f"{flag} applies only to the S3 emulation Job (--driver kubernetes --artifact-backend s3)")
    if s3_backend and action == "full-run":
        if config.blocksci.workflow == "cached":
            errors.append("full-run cannot reuse a cache before emulation; use --blocksci-workflow reusable")
        # Any analyzer subset runs, emulation alone included. Mappings read the
        # coinjoin-analysis baseline, which a fresh run has only if it computes it.
        if stages.mappings and not stages.analysis:
            errors.append("full-run --artifact-backend s3 --mappings-pbs requires --analysis-pbs")
        for flag, path, value in (
            ("--parallel", "parallel", config.parallel),
            ("--blocksci-script", "blocksci.script", config.blocksci.script),
        ):
            if _supplied(config, path, value):
                errors.append(f"full-run --artifact-backend s3 does not support {flag}")
    return errors


def _remaining_errors(config: PipelineConfiguration, stages: _PbsStages) -> list[str]:
    errors: list[str] = []
    blocksci = config.blocksci
    if (
        config.action == "full-run"
        and config.artifacts.backend != "s3"
        and (blocksci.workflow != "combined" or blocksci.task != "detect")
    ):
        errors.append("reusable BlockSci workflows are currently supported only with the S3 artifact backend")
    if blocksci.script and not Path(blocksci.script).expanduser().is_file():
        errors.append(f"BlockSci script not found: {blocksci.script}")
    if stages.mappings and config.engine != "wasabi":
        errors.append("--mappings-pbs is supported only with --engine wasabi")
    if stages.mappings and config.coinjoin_type != "wasabi2":
        errors.append("--mappings-pbs requires --coinjoin-type wasabi2")
    return errors
