"""Cross-field policy shared by YAML and CLI configuration inputs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from exporters.verify_chain import is_block_hash

from .storage.s3 import RUN_ID_RE

if TYPE_CHECKING:
    from .configuration import PipelineConfiguration, PBSResourceConfiguration

# Alias -> canonical action. Aliases are accepted but never named in error text.
ACTION_ALIASES = {"coinjoin": "coinjoin-analysis"}


def _supplied(config: PipelineConfiguration, path: str, value: object) -> bool:
    """Explicit false/null is inactive; zero and empty strings remain supplied."""
    return path in config.provided and value is not None and value is not False and value != ()


def _resources_supplied(config: PipelineConfiguration, stage: str, resources: PBSResourceConfiguration) -> bool:
    return any(
        _supplied(config, f"pbs.{stage}.{name}", value)
        for name, value in (
            ("ncpus", resources.ncpus),
            ("mem", resources.mem),
            ("scratch", resources.scratch),
            ("walltime", resources.walltime),
        )
    )


def cross_option_errors(config: PipelineConfiguration) -> list[str]:
    """Return violated rules in order, retaining CLI spellings in diagnostics."""
    action = config.action
    analysis_pbs = _supplied(config, "stages.analysis", config.stages.analysis)
    blocksci_pbs = _supplied(config, "stages.blocksci", config.stages.blocksci)
    mappings_pbs = _supplied(config, "stages.mappings", config.stages.mappings)
    errors: list[str] = []
    if config.run_dir is not None and config.all_runs:
        errors.append("--run-dir and --all-runs are mutually exclusive")
    if action in {"analyze", "export", "coinjoin-analysis", "coinjoin", "mappings"}:
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
        if run_id is not None and (len(run_id) > 63 or ".." in run_id or not RUN_ID_RE.fullmatch(run_id)):
            errors.append(
                f"{flag} must be at most 63 characters, begin and end with an "
                "alphanumeric character, contain only [A-Za-z0-9._-], and not "
                "contain '..'"
            )
    if config.driver != "kubernetes":
        for flag, path, value in (
            ("--kubeconfig", "kubernetes.kubeconfig", config.kubernetes.kubeconfig),
            ("--namespace", "kubernetes.namespace", config.kubernetes.namespace),
            ("--reuse-namespace", "kubernetes.reuse_namespace", config.kubernetes.reuse_namespace),
            ("--copy-to-host", "kubernetes.copy_to_host", config.kubernetes.copy_to_host),
        ):
            if _supplied(config, path, value):
                errors.append(f"{flag} requires --driver kubernetes")
    for flag, enabled, permitted in (
        ("--analysisPbs", analysis_pbs, ("full-run", "coinjoin-analysis", "pbs-from-s3")),
        ("--blocksciPbs", blocksci_pbs, ("full-run", "analyze", "pbs-from-s3")),
        ("--mappingsPbs", mappings_pbs, ("full-run", "mappings", "pbs-from-s3")),
    ):
        if enabled and action not in permitted:
            errors.append(f"{flag} is supported only by {', '.join(permitted)}")
    for stage, resources, enabled, flag in (
        ("analysis", config.pbs.analysis, analysis_pbs, "--analysisPbs"),
        ("blocksci", config.pbs.blocksci, blocksci_pbs, "--blocksciPbs"),
        ("mappings", config.pbs.mappings, mappings_pbs, "--mappingsPbs"),
    ):
        if _resources_supplied(config, stage, resources) and not enabled:
            errors.append(f"{stage}-specific PBS resources require {flag}")
    backend = config.artifacts.backend
    blocksci_workflow = config.blocksci.workflow
    blocksci_task = config.blocksci.task
    if blocksci_workflow != "combined" and not blocksci_pbs:
        errors.append("reusable BlockSci workflows require --blocksciPbs")
    if blocksci_task == "parse":
        if action != "pbs-from-s3" or blocksci_workflow != "reusable":
            errors.append("--blocksci-task parse requires pbs-from-s3 --blocksci-workflow reusable")
        if analysis_pbs or not blocksci_pbs:
            errors.append("--blocksci-task parse requires --blocksciPbs without --analysisPbs")
    elif blocksci_task == "update":
        if action != "pbs-from-s3" or blocksci_workflow != "cached":
            errors.append("--blocksci-task update requires pbs-from-s3 --blocksci-workflow cached")
        if analysis_pbs or not blocksci_pbs:
            errors.append("--blocksci-task update requires --blocksciPbs without --analysisPbs")
    elif blocksci_task not in {"detect", "external"}:
        if action != "pbs-from-s3":
            errors.append("BlockSci reusable tasks are submitted with pbs-from-s3")
        if blocksci_workflow == "combined":
            errors.append("BlockSci reusable tasks require --blocksci-workflow reusable or cached")
        if analysis_pbs or not blocksci_pbs:
            errors.append("BlockSci reusable tasks require --blocksciPbs without --analysisPbs")
    if action == "pbs-from-s3" and blocksci_task == "script" and not config.blocksci.script:
        errors.append("--blocksci-task script requires --blocksci-script")
    if (
        action == "pbs-from-s3"
        and blocksci_task != "script"
        and _supplied(config, "blocksci.script", config.blocksci.script)
    ):
        errors.append("--blocksci-script requires --blocksci-task script")
    if blocksci_task != "notebook" and _supplied(config, "blocksci.notebooks_dir", config.blocksci.notebooks_dir):
        errors.append("--blocksci-notebooks-dir requires --blocksci-task notebook")
    if blocksci_task != "notebook" and _supplied(config, "blocksci.notebook_port", config.blocksci.notebook_port):
        errors.append("--blocksci-notebook-port requires --blocksci-task notebook")
    external_bitcoin = bool(config.blocksci.external_bitcoin_datadir)
    bitcoin_blocks_uri = bool(config.blocksci.bitcoin_blocks_uri)
    external_index = bool(config.blocksci.external_blocksci_dir)
    external_network = _supplied(config, "blocksci.network", config.blocksci.network)
    external_max_block = _supplied(config, "blocksci.max_block", config.blocksci.max_block)
    source_cache_run_id = config.blocksci.cache_source_run_id
    if blocksci_task == "update":
        if not source_cache_run_id:
            errors.append("--blocksci-task update requires --blocksci-cache-source-run-id")
        if not (external_bitcoin or bitcoin_blocks_uri):
            errors.append(
                "--blocksci-task update requires --blocksci-external-bitcoin-datadir or --blocksci-bitcoin-blocks-uri"
            )
        if external_index:
            errors.append("--blocksci-task update does not support --blocksci-external-blocksci-dir")
        target_run_id = config.run_id
        if source_cache_run_id and target_run_id == source_cache_run_id:
            errors.append("--blocksci-cache-source-run-id must differ from target --run-id")
    elif source_cache_run_id:
        errors.append("--blocksci-cache-source-run-id requires --blocksci-task update")
    if sum((external_bitcoin, bitcoin_blocks_uri, external_index)) > 1:
        errors.append(
            "choose only one BlockSci source: --blocksci-external-bitcoin-datadir, "
            "--blocksci-bitcoin-blocks-uri, or --blocksci-external-blocksci-dir"
        )
    if external_bitcoin or bitcoin_blocks_uri or external_index:
        parse_source = action == "pbs-from-s3" and blocksci_workflow == "reusable" and blocksci_task == "parse"
        update_source = (
            action == "pbs-from-s3"
            and blocksci_workflow == "cached"
            and blocksci_task == "update"
            and (external_bitcoin or bitcoin_blocks_uri)
            and not external_index
        )
        if not (parse_source or update_source):
            errors.append("external BlockSci sources require either reusable parse or cached update")
    if external_bitcoin or bitcoin_blocks_uri:
        if not external_network or not external_max_block:
            errors.append("an external Bitcoin source requires --blocksci-network and --blocksci-max-block")
    elif external_network or external_max_block:
        errors.append("--blocksci-network and --blocksci-max-block require an external Bitcoin source")
    expected_hash = config.blocksci.expected_block_hash
    if expected_hash is not None:
        if not bitcoin_blocks_uri:
            errors.append("--blocksci-expected-block-hash requires --blocksci-bitcoin-blocks-uri")
        if not is_block_hash(expected_hash):
            errors.append("--blocksci-expected-block-hash must be 64 lowercase hexadecimal characters")
    if bitcoin_blocks_uri and config.blocksci.network == "bitcoin" and expected_hash is None:
        errors.append("Mainnet block archives require --blocksci-expected-block-hash at --blocksci-max-block")
    external_baseline_uri = bool(config.blocksci.external_baseline_uri)
    if blocksci_task == "external":
        if action != "pbs-from-s3" or blocksci_workflow == "combined":
            errors.append("--blocksci-task external requires pbs-from-s3 with reusable or cached workflow")
        if analysis_pbs or not blocksci_pbs:
            errors.append("--blocksci-task external requires --blocksciPbs without --analysisPbs")
        if not external_baseline_uri:
            errors.append("--blocksci-task external requires --external-baseline-uri")
    elif external_baseline_uri:
        errors.append("--external-baseline-uri requires --blocksci-task external")
    if action == "pbs-from-s3":
        for flag, value in (
            ("--run-id", config.run_id),
            ("--artifact-uri", config.artifacts.uri),
            ("--s3-endpoint-url", config.artifacts.endpoint_url),
            ("--s3-credentials-file", config.artifacts.credentials_file),
            ("--s3-profile", config.artifacts.profile),
            ("--engine", config.engine),
        ):
            if not value:
                errors.append(f"pbs-from-s3 requires {flag}")
        if not any((analysis_pbs, blocksci_pbs, mappings_pbs)):
            errors.append("pbs-from-s3 requires --analysisPbs, --blocksciPbs, or --mappingsPbs")
        separate_report = blocksci_pbs and blocksci_task == "detect"
        if _resources_supplied(config, "unified_report", config.pbs.unified_report) and not separate_report:
            errors.append("unified-report PBS resource overrides require a separate unified-report job")
    if backend == "s3" and action == "full-run":
        if blocksci_workflow == "cached":
            errors.append("full-run cannot reuse a cache before emulation; use --blocksci-workflow reusable")
        if config.driver != "kubernetes":
            errors.append("full-run --artifact-backend s3 requires --driver kubernetes")
        for flag, value in (
            ("--run-id", config.run_id),
            ("--artifact-uri", config.artifacts.uri),
            ("--s3-endpoint-url", config.artifacts.endpoint_url),
            ("--s3-secret-name", config.artifacts.secret_name),
            ("--s3-credentials-file", config.artifacts.credentials_file),
            ("--s3-profile", config.artifacts.profile),
        ):
            if not value:
                errors.append(f"full-run --artifact-backend s3 requires {flag}")
        if not analysis_pbs or not blocksci_pbs:
            errors.append("full-run --artifact-backend s3 requires both --analysisPbs and --blocksciPbs")
        if not _supplied(config, "kubernetes.reuse_namespace", config.kubernetes.reuse_namespace):
            errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created"
            )
        if _supplied(config, "parallel", config.parallel):
            errors.append("full-run --artifact-backend s3 does not support --parallel")
        if _supplied(config, "blocksci.script", config.blocksci.script):
            errors.append("full-run --artifact-backend s3 does not support --blocksci-script")
        for flag, path, value in (
            ("--kubernetes-btc-datadir", "kubernetes.btc_datadir", config.kubernetes.btc_datadir),
            ("--pbs-bitcoin-datadir", "pbs.bitcoin_datadir", config.pbs.bitcoin_datadir),
            ("--copy-to-host", "kubernetes.copy_to_host", config.kubernetes.copy_to_host),
        ):
            if _supplied(config, path, value):
                errors.append(f"Kubernetes S3-compatible mode does not support {flag}")
    if backend == "s3" and action == "emulate":
        for flag, value in (
            ("--run-id", config.run_id),
            ("--artifact-uri", config.artifacts.uri),
            ("--s3-endpoint-url", config.artifacts.endpoint_url),
            ("--s3-secret-name", config.artifacts.secret_name),
            ("--s3-credentials-file", config.artifacts.credentials_file),
            ("--s3-profile", config.artifacts.profile),
        ):
            if not value:
                errors.append(f"Kubernetes S3-compatible mode requires {flag}")
        if config.driver != "kubernetes":
            errors.append("--artifact-backend s3 requires --driver kubernetes")
        if not _supplied(config, "kubernetes.reuse_namespace", config.kubernetes.reuse_namespace):
            errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created"
            )
        for flag, path, value in (
            ("--kubernetes-btc-datadir", "kubernetes.btc_datadir", config.kubernetes.btc_datadir),
            ("--pbs-bitcoin-datadir", "pbs.bitcoin_datadir", config.pbs.bitcoin_datadir),
            ("--copy-to-host", "kubernetes.copy_to_host", config.kubernetes.copy_to_host),
        ):
            if _supplied(config, path, value):
                errors.append(f"Kubernetes S3-compatible mode does not support {flag}")
    if action == "full-run" and backend != "s3" and (blocksci_workflow != "combined" or blocksci_task != "detect"):
        errors.append("reusable BlockSci workflows are currently supported only with the S3 artifact backend")
    engine = config.engine
    if engine is not None and engine not in {"wasabi", "joinmarket"}:
        errors.append("--engine must be wasabi or joinmarket")
    script = config.blocksci.script
    if script and not Path(script).expanduser().is_file():
        errors.append(f"BlockSci script not found: {script}")
    if mappings_pbs and config.engine not in (None, "wasabi"):
        errors.append("--mappingsPbs is supported only with --engine wasabi")
    if mappings_pbs and (config.coinjoin_type or "wasabi2") != "wasabi2":
        errors.append("--mappingsPbs requires --coinjoin-type wasabi2")
    if action == "mappings" and not mappings_pbs:
        errors.append("mappings requires --mappingsPbs")
    return errors
