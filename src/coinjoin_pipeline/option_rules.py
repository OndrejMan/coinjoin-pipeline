"""Cross-field policy shared by YAML and CLI configuration inputs."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

from exporters.verify_chain import is_block_hash

from .configuration import cli_flag, config_value, option_specs
from .execution.pbs.defaults import MAINNET_SYNC_RESOURCES
from .execution.stages import runs_unified_report
from .storage.s3 import RUN_ID_RULE, is_valid_run_id

if TYPE_CHECKING:
    from .configuration import PipelineConfiguration, PBSResourceConfiguration

# Tasks that may read the index of another run (--blocksci-cache-source-run-id) without writing to it.
READ_ONLY_CACHE_TASKS = frozenset({"script", "external", "notebook"})
PERSISTENT_INDEX_TASKS = frozenset({"parse", "update", "backup"}) | READ_ONLY_CACHE_TASKS
# Resource groups only the mainnet synchronization submits; other actions use pbs.blocksci too.
MAINNET_ONLY_PBS_GROUPS = tuple(f"pbs.{stage}." for stage in MAINNET_SYNC_RESOURCES if stage != "blocksci")
SHARED_ROOTS = ("/storage/", "/auto/")
# The jobs that only maintain the index; their state can live at one fixed S3 prefix.
INDEX_STATE_TASKS = ("parse", "update", "backup")
MULTIJOB_ROOT = "/scratch/multijob-{}/"


def _supplied(config: PipelineConfiguration, path: str, value: object) -> bool:
    """Explicit false/null is inactive; zero and empty strings remain supplied."""
    return path in config.provided and value is not None and value is not False and value != ()


def _resources_supplied(config: PipelineConfiguration, stage: str, resources: PBSResourceConfiguration) -> bool:
    return any(
        _supplied(config, f"pbs.{stage}.{item.name}", getattr(resources, item.name)) for item in fields(resources)
    )


def _persistent_index_errors(config: PipelineConfiguration) -> list[str]:
    """Rules for a BlockSci index kept in a directory shared by jobs instead of the S3 cache."""
    errors: list[str] = []
    blocksci = config.blocksci
    index_dir, backup_uri = blocksci.index_dir, blocksci.index_backup_uri
    multijob = config.pbs.blocksci.multijob
    for stage, resources in (
        ("analysis", config.pbs.analysis),
        ("mappings", config.pbs.mappings),
        ("unified-report", config.pbs.unified_report),
    ):
        if _supplied(config, f"pbs.{stage.replace('-', '_')}.multijob", resources.multijob):
            errors.append(f"--pbs-{stage}-multijob is supported only for the BlockSci stage")
    if blocksci.index_state_uri and (not index_dir or blocksci.task not in INDEX_STATE_TASKS):
        errors.append(
            "--blocksci-index-state-uri requires --blocksci-index-dir and --blocksci-task parse, update or backup"
        )
    if not index_dir:
        if backup_uri:
            errors.append("--blocksci-index-backup-uri requires --blocksci-index-dir")
        if multijob:
            errors.append("--pbs-blocksci-multijob requires --blocksci-index-dir on that multijob scratch")
        if blocksci.task == "backup":
            errors.append("--blocksci-task backup requires --blocksci-index-dir")
        return errors
    if blocksci.task not in PERSISTENT_INDEX_TASKS:
        errors.append(
            "--blocksci-index-dir requires --blocksci-task parse, update, backup, script, external, or notebook"
        )
    if blocksci.cache_source_run_id:
        errors.append("choose one index source: --blocksci-index-dir or --blocksci-cache-source-run-id")
    if blocksci.task == "parse" and not (blocksci.bitcoin_blocks_uri or blocksci.external_bitcoin_datadir):
        errors.append(
            "a persistent index is parsed from --blocksci-bitcoin-blocks-uri or --blocksci-external-bitcoin-datadir"
        )
    on_multijob = next(
        (place for place in ("brno", "plzen", "global") if index_dir.startswith(MULTIJOB_ROOT.format(place))), None
    )
    if on_multijob is None and not index_dir.startswith(SHARED_ROOTS):
        errors.append("--blocksci-index-dir must be under /scratch/multijob-<location>/ or /storage/")
    elif on_multijob is not None and multijob != on_multijob:
        errors.append(f"--blocksci-index-dir on multijob-{on_multijob} requires --pbs-blocksci-multijob {on_multijob}")
    elif on_multijob is None and multijob:
        errors.append(
            f"--pbs-blocksci-multijob {multijob} requires --blocksci-index-dir under /scratch/multijob-{multijob}/"
        )
    if ".." in Path(index_dir).parts:
        errors.append("--blocksci-index-dir must not contain '..'")
    if blocksci.task == "backup" and not backup_uri:
        errors.append("--blocksci-task backup requires --blocksci-index-backup-uri")
    if blocksci.index_in_scratch and (not backup_uri or blocksci.task not in {"script", "external", "notebook"}):
        errors.append(
            "--blocksci-index-in-scratch requires --blocksci-index-backup-uri and --blocksci-task "
            "script, external or notebook"
        )
    if blocksci.restore_index and (not backup_uri or blocksci.task != "notebook" or blocksci.index_in_scratch):
        errors.append(
            "--blocksci-restore-index requires --blocksci-index-backup-uri and --blocksci-task notebook, "
            "without --blocksci-index-in-scratch"
        )
    if blocksci.storage_report_uri and not blocksci.restore_index:
        errors.append("--blocksci-storage-report-uri requires --blocksci-restore-index")
    if blocksci.dumplings_uri and not (blocksci.index_in_scratch or blocksci.restore_index):
        errors.append("--blocksci-dumplings-uri requires --blocksci-index-in-scratch or --blocksci-restore-index")
    if blocksci.dumplings_dir and (
        blocksci.task != "notebook"
        or not blocksci.index_dir
        or blocksci.index_in_scratch
        or blocksci.restore_index
    ):
        errors.append(
            "--blocksci-dumplings-dir requires --blocksci-task notebook and --blocksci-index-dir, "
            "without --blocksci-index-in-scratch or --blocksci-restore-index"
        )
    return errors


def cross_option_errors(config: PipelineConfiguration) -> list[str]:
    """Return every violated rule in order, retaining CLI spellings in diagnostics."""
    if config.action == "mainnet-sync":
        from .execution.mainnet_sync import configuration_errors

        errors = configuration_errors(config)
    elif any(path.startswith(("mainnet.", *MAINNET_ONLY_PBS_GROUPS)) for path in config.provided):
        groups = ", ".join(group.rstrip(".") for group in MAINNET_ONLY_PBS_GROUPS)
        errors = [f"mainnet, {groups} settings require action: mainnet-sync"]
    else:
        errors = _action_errors(config)
    return errors + _general_errors(config)


def _general_errors(config: PipelineConfiguration) -> list[str]:
    """Rules that hold for every action, mainnet-sync included."""
    errors: list[str] = []
    for path, destination, item in option_specs():
        permitted = item.metadata.get("actions")
        if permitted and path in config.provided and config.action not in permitted:
            value = config_value(config, path)
            if value is not None and value is not False:
                errors.append(f"{cli_flag(destination)} is not supported for {config.action}")
    if config.action == "external analyze" and not config.run_id:
        errors.append("external analyze requires --run-id")
    if config.all_runs and config.stages.analysis:
        errors.append("--all-runs is not supported with --analysisPbs; select one run")
    if not 1024 <= config.blocksci.notebook_port <= 65535:
        errors.append("--blocksci-notebook-port must be between 1024 and 65535")
    if config.artifacts.backend == "s3" and config.action != "pbs-from-s3":
        for flag, value in (
            ("--kubernetes-btc-datadir", config.kubernetes.btc_datadir),
            ("--pbs-bitcoin-datadir", config.pbs.bitcoin_datadir),
            ("--copy-to-host", config.kubernetes.copy_to_host),
        ):
            if value:
                errors.append(f"Kubernetes S3-compatible mode does not support shared Bitcoin storage ({flag})")
    if config.driver == "kubernetes" and config.stages.blocksci and not config.kubernetes.copy_to_host:
        first, second = config.kubernetes.btc_datadir, config.pbs.bitcoin_datadir
        if first and second and Path(first).expanduser().resolve() != Path(second).expanduser().resolve():
            errors.append("Kubernetes and PBS Bitcoin datadirs must identify the same directory")
    external = config.external
    if config.action != "external analyze" and external.configured:
        errors.append("external settings require action: external analyze")
    if config.action == "external analyze":
        supplied = external.bitcoin_datadir is not None or external.baseline is not None
        if external.resume and supplied:
            errors.append("external.resume cannot be combined with external.bitcoin_datadir or external.baseline")
        if not external.resume and (not external.bitcoin_datadir or not external.baseline):
            errors.append("a new external analysis requires external.bitcoin_datadir and external.baseline")
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
    """Rules of the emulation and analysis actions (everything except mainnet-sync)."""
    from .execution.coinjoin_tools import option_errors

    stages = _PbsStages.requested(config)
    return [
        *option_errors(config),
        *_run_selection_errors(config),
        *_driver_errors(config),
        *_pbs_stage_errors(config, stages),
        *_blocksci_task_errors(config, stages),
        *_cache_source_errors(config),
        *_persistent_index_errors(config),
        *_dumplings_errors(config, stages),
        *_height_errors(config),
        *_blocksci_source_errors(config),
        *_external_baseline_errors(config, stages),
        *_s3_errors(config, stages),
        *_remaining_errors(config, stages),
    ]


def _run_selection_errors(config: PipelineConfiguration) -> list[str]:
    errors: list[str] = []
    action = config.action
    if config.run_dir is not None and config.all_runs:
        errors.append("--run-dir and --all-runs are mutually exclusive")
    if action in {"analyze", "export", "coinjoin-analysis", "mappings"}:
        if not _supplied(config, "run_dir", config.run_dir) and not _supplied(config, "all_runs", config.all_runs):
            errors.append(f"{action} requires --run-dir (or --all-runs where supported)")
    if (
        action == "clean"
        and not _supplied(config, "dry_run", config.dry_run)
        and not _supplied(config, "yes", config.yes)
    ):
        errors.append("clean is destructive; pass --yes or --dry-run")
    for flag, run_id in (
        ("--run-id", config.run_id),
        ("--blocksci-cache-source-run-id", config.blocksci.cache_source_run_id),
    ):
        if run_id is not None and not is_valid_run_id(run_id):
            errors.append(f"{flag} {RUN_ID_RULE}")
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
        ("--analysisPbs", stages.analysis, ("full-run", "coinjoin-analysis", "pbs-from-s3")),
        ("--blocksciPbs", stages.blocksci, ("full-run", "analyze", "pbs-from-s3")),
        ("--mappingsPbs", stages.mappings, ("full-run", "mappings", "pbs-from-s3")),
    ):
        if enabled and config.action not in permitted:
            errors.append(f"{flag} is supported only by {', '.join(permitted)}")
    for stage, resources, enabled, flag in (
        ("analysis", config.pbs.analysis, stages.analysis, "--analysisPbs"),
        ("blocksci", config.pbs.blocksci, stages.blocksci, "--blocksciPbs"),
        ("mappings", config.pbs.mappings, stages.mappings, "--mappingsPbs"),
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
        errors.append("reusable BlockSci workflows require --blocksciPbs")
    if task in {"parse", "update"}:
        required_workflow = "reusable" if task == "parse" else "cached"
        if action != "pbs-from-s3" or workflow != required_workflow:
            errors.append(f"--blocksci-task {task} requires pbs-from-s3 --blocksci-workflow {required_workflow}")
        if not stages.blocksci_alone:
            errors.append(f"--blocksci-task {task} requires --blocksciPbs without --analysisPbs")
    elif task not in {"detect", "external"}:
        if action != "pbs-from-s3":
            errors.append("BlockSci reusable tasks are submitted with pbs-from-s3")
        if workflow == "combined":
            errors.append("BlockSci reusable tasks require --blocksci-workflow reusable or cached")
        if not stages.blocksci_alone:
            errors.append("BlockSci reusable tasks require --blocksciPbs without --analysisPbs")
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


def _cache_source_errors(config: PipelineConfiguration) -> list[str]:
    """Rules for reading the index of another run (--blocksci-cache-source-run-id)."""
    errors: list[str] = []
    blocksci = config.blocksci
    source_run_id = blocksci.cache_source_run_id
    if blocksci.task == "update":
        if not source_run_id and not blocksci.index_dir:
            errors.append("--blocksci-task update requires --blocksci-cache-source-run-id or --blocksci-index-dir")
        if not (blocksci.external_bitcoin_datadir or blocksci.bitcoin_blocks_uri):
            errors.append(
                "--blocksci-task update requires --blocksci-external-bitcoin-datadir or --blocksci-bitcoin-blocks-uri"
            )
        if blocksci.external_blocksci_dir:
            errors.append("--blocksci-task update does not support --blocksci-external-blocksci-dir")
        if source_run_id and config.run_id == source_run_id:
            errors.append("--blocksci-cache-source-run-id must differ from target --run-id")
    elif blocksci.task in READ_ONLY_CACHE_TASKS and source_run_id:
        # The analysis only reads the source cache; its outputs and markers belong to its own run.
        if blocksci.workflow != "cached":
            errors.append("--blocksci-cache-source-run-id with an analysis task requires --blocksci-workflow cached")
        if config.run_id == source_run_id:
            errors.append("--blocksci-cache-source-run-id must differ from target --run-id")
    elif source_run_id:
        errors.append("--blocksci-cache-source-run-id requires --blocksci-task update, script, external, or notebook")
    return errors


def _dumplings_errors(config: PipelineConfiguration, stages: _PbsStages) -> list[str]:
    if not config.dumplings_uri:
        return []
    errors: list[str] = []
    if config.action != "pbs-from-s3" or not stages.analysis:
        errors.append("--dumplings-uri requires pbs-from-s3 --analysisPbs")
    if stages.mappings:
        errors.append("--dumplings-uri does not support --mappingsPbs")
    return errors


def _height_errors(config: PipelineConfiguration) -> list[str]:
    errors: list[str] = []
    blocksci = config.blocksci
    pin_height, scan_start, scan_end = blocksci.pin_height, blocksci.scan_start_height, blocksci.scan_end_height
    if blocksci.task not in {"script", "external"}:
        for flag, path, height in (
            ("--blocksci-pin-height", "blocksci.pin_height", pin_height),
            ("--blocksci-scan-start-height", "blocksci.scan_start_height", scan_start),
            ("--blocksci-scan-end-height", "blocksci.scan_end_height", scan_end),
        ):
            if _supplied(config, path, height):
                errors.append(f"{flag} requires --blocksci-task script or external")
    if scan_start is not None and scan_end is not None and scan_start >= scan_end:
        errors.append("--blocksci-scan-start-height must be below --blocksci-scan-end-height")
    if pin_height is not None and scan_end is not None and scan_end > pin_height + 1:
        errors.append("--blocksci-scan-end-height must not exceed --blocksci-pin-height + 1")
    return errors


def _blocksci_source_errors(config: PipelineConfiguration) -> list[str]:
    """Rules for parsing or updating an index from an external Bitcoin or BlockSci source."""
    errors: list[str] = []
    blocksci = config.blocksci
    external_bitcoin = bool(blocksci.external_bitcoin_datadir)
    blocks_uri = bool(blocksci.bitcoin_blocks_uri)
    external_index = bool(blocksci.external_blocksci_dir)
    bitcoin_source = external_bitcoin or blocks_uri
    if sum((external_bitcoin, blocks_uri, external_index)) > 1:
        errors.append(
            "choose only one BlockSci source: --blocksci-external-bitcoin-datadir, "
            "--blocksci-bitcoin-blocks-uri, or --blocksci-external-blocksci-dir"
        )
    if bitcoin_source or external_index:
        on_s3 = config.action == "pbs-from-s3"
        parse_source = on_s3 and blocksci.workflow == "reusable" and blocksci.task == "parse"
        update_source = (
            on_s3
            and blocksci.workflow == "cached"
            and blocksci.task == "update"
            and bitcoin_source
            and not external_index
        )
        if not (parse_source or update_source):
            errors.append("external BlockSci sources require either reusable parse or cached update")
    network_supplied = _supplied(config, "blocksci.network", blocksci.network)
    max_block_supplied = _supplied(config, "blocksci.max_block", blocksci.max_block)
    if bitcoin_source:
        if not network_supplied or not max_block_supplied:
            errors.append("an external Bitcoin source requires --blocksci-network and --blocksci-max-block")
    elif network_supplied or max_block_supplied:
        errors.append("--blocksci-network and --blocksci-max-block require an external Bitcoin source")
    expected_hash = blocksci.expected_block_hash
    if expected_hash is not None:
        if not blocks_uri:
            errors.append("--blocksci-expected-block-hash requires --blocksci-bitcoin-blocks-uri")
        if not is_block_hash(expected_hash):
            errors.append("--blocksci-expected-block-hash must be 64 lowercase hexadecimal characters")
    if blocks_uri and blocksci.network == "bitcoin" and expected_hash is None:
        errors.append("Mainnet block archives require --blocksci-expected-block-hash at --blocksci-max-block")
    return errors


def _external_baseline_errors(config: PipelineConfiguration, stages: _PbsStages) -> list[str]:
    errors: list[str] = []
    blocksci = config.blocksci
    if blocksci.task == "external":
        if config.action != "pbs-from-s3" or blocksci.workflow == "combined":
            errors.append("--blocksci-task external requires pbs-from-s3 with reusable or cached workflow")
        if not stages.blocksci_alone:
            errors.append("--blocksci-task external requires --blocksciPbs without --analysisPbs")
        if not blocksci.external_baseline_uri:
            errors.append("--blocksci-task external requires --external-baseline-uri")
    elif blocksci.external_baseline_uri:
        errors.append("--external-baseline-uri requires --blocksci-task external")
    if blocksci.external_false_cjtxs_uri and blocksci.task != "external":
        errors.append("--external-false-cjtxs-uri requires --blocksci-task external")
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
            errors.append("pbs-from-s3 requires --analysisPbs, --blocksciPbs, or --mappingsPbs")
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
    if s3_backend and action == "full-run":
        if config.blocksci.workflow == "cached":
            errors.append("full-run cannot reuse a cache before emulation; use --blocksci-workflow reusable")
        # Any analyzer subset runs, emulation alone included. Mappings read the
        # coinjoin-analysis baseline, which a fresh run has only if it computes it.
        if stages.mappings and not stages.analysis:
            errors.append("full-run --artifact-backend s3 --mappingsPbs requires --analysisPbs")
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
        errors.append("--mappingsPbs is supported only with --engine wasabi")
    if stages.mappings and config.coinjoin_type != "wasabi2":
        errors.append("--mappingsPbs requires --coinjoin-type wasabi2")
    if config.action == "mappings" and not stages.mappings:
        errors.append("mappings requires --mappingsPbs")
    return errors
