"""Cross-option rules shared by every way a pipeline command is assembled.

The host CLI validates argv against these rules before it starts the wrapper,
YAML configurations are flattened to argv and validated the same way, the
wrapper entrypoint re-runs the host validator on its own argv, and the
interactive command builder applies them to the command it is composing.
Each input path supplies an :class:`OptionView`; the rules never see how the
options were written down, so a rule changed here changes for all of them.

Only rules decidable from the explicit options belong here.  The wrapper keeps
the checks that need its effective values: environment-derived defaults such
as ``PBS_BITCOIN_DATADIR``, and normalizing validators for URIs, run IDs, and
credential files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

# Alias -> canonical action. Aliases are accepted but never named in error text.
ACTION_ALIASES = {"coinjoin": "coinjoin-analysis"}
# Actions each PBS offload flag may accompany; also the source of the error text.
PBS_STAGE_ACTIONS = {
    "--analysisPbs": ("full-run", "coinjoin-analysis", "coinjoin", "pbs-from-s3"),
    "--blocksciPbs": ("full-run", "analyze", "pbs-from-s3"),
    "--mappingsPbs": ("full-run", "mappings", "pbs-from-s3"),
}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


class OptionView(Protocol):
    """Read-only access to the explicit options of one command."""

    def has(self, flag: str) -> bool:
        """Whether ``flag`` was given, with or without a value."""

    def value(self, flag: str) -> str | None:
        """The value of the last occurrence of ``flag``, or ``None``."""


def _given(options: OptionView, flag: str) -> bool:
    """Whether a value-taking option carries a non-empty value.

    Requirements are checked this way because the wrapper acts on argparse's
    effective value, for which ``--artifact-uri ''`` is as good as missing.
    """
    return bool(options.value(flag))


def cross_option_errors(options: OptionView, action: str) -> list[str]:
    """Return every violated cross-option rule for ``action``, in rule order."""
    errors: list[str] = []
    if action in {"analyze", "export", "coinjoin-analysis", "coinjoin", "mappings"}:
        if not options.has("--run-dir") and not options.has("--all-runs"):
            errors.append(f"{action} requires --run-dir (or --all-runs where supported)")
    if action == "clean" and not options.has("--dry-run") and not options.has("--yes"):
        errors.append("clean is destructive; pass --yes or --dry-run")
    for flag in ("--run-id", "--blocksci-cache-source-run-id"):
        run_id = options.value(flag)
        if run_id is not None and (
            len(run_id) > 63 or ".." in run_id or not RUN_ID_RE.fullmatch(run_id)
        ):
            errors.append(
                f"{flag} must be at most 63 characters, begin and end with an "
                "alphanumeric character, contain only [A-Za-z0-9._-], and not "
                "contain '..'"
            )
    if options.value("--driver") != "kubernetes":
        for flag in ("--kubeconfig", "--namespace", "--reuse-namespace", "--copy-to-host"):
            if options.has(flag):
                errors.append(f"{flag} requires --driver kubernetes")
    for flag, permitted in PBS_STAGE_ACTIONS.items():
        if options.has(flag) and action not in permitted:
            supported = ", ".join(name for name in permitted if name not in ACTION_ALIASES)
            errors.append(f"{flag} is supported only by {supported}")
    for stage, enabling_flag in (
        ("analysis", "--analysisPbs"),
        ("blocksci", "--blocksciPbs"),
        ("mappings", "--mappingsPbs"),
    ):
        stage_resources = tuple(
            f"--pbs-{stage}-{resource}"
            for resource in ("ncpus", "mem", "scratch", "walltime")
        )
        if any(options.has(flag) for flag in stage_resources) and not options.has(
            enabling_flag
        ):
            errors.append(f"{stage}-specific PBS resources require {enabling_flag}")
    backend = options.value("--artifact-backend") or "shared-storage"
    blocksci_workflow = options.value("--blocksci-workflow") or "combined"
    blocksci_task = options.value("--blocksci-task") or "detect"
    if blocksci_workflow != "combined" and not options.has("--blocksciPbs"):
        errors.append("reusable BlockSci workflows require --blocksciPbs")
    if blocksci_task == "parse":
        if action != "pbs-from-s3" or blocksci_workflow != "reusable":
            errors.append(
                "--blocksci-task parse requires pbs-from-s3 --blocksci-workflow reusable"
            )
        if options.has("--analysisPbs") or not options.has("--blocksciPbs"):
            errors.append(
                "--blocksci-task parse requires --blocksciPbs without --analysisPbs"
            )
    elif blocksci_task == "update":
        if action != "pbs-from-s3" or blocksci_workflow != "cached":
            errors.append(
                "--blocksci-task update requires pbs-from-s3 --blocksci-workflow cached"
            )
        if options.has("--analysisPbs") or not options.has("--blocksciPbs"):
            errors.append(
                "--blocksci-task update requires --blocksciPbs without --analysisPbs"
            )
    elif blocksci_task not in {"detect", "external"}:
        if action != "pbs-from-s3":
            errors.append("BlockSci reusable tasks are submitted with pbs-from-s3")
        if blocksci_workflow == "combined":
            errors.append(
                "BlockSci reusable tasks require --blocksci-workflow reusable or cached"
            )
        if options.has("--analysisPbs") or not options.has("--blocksciPbs"):
            errors.append(
                "BlockSci reusable tasks require --blocksciPbs without --analysisPbs"
            )
    if action == "pbs-from-s3" and blocksci_task == "script" and not (
        _given(options, "--blocksci-script") or _given(options, "--blocksciScript")
    ):
        errors.append("--blocksci-task script requires --blocksci-script")
    if action == "pbs-from-s3" and blocksci_task != "script" and (
        options.has("--blocksci-script") or options.has("--blocksciScript")
    ):
        errors.append("--blocksci-script requires --blocksci-task script")
    if blocksci_task != "notebook" and options.has("--blocksci-notebooks-dir"):
        errors.append("--blocksci-notebooks-dir requires --blocksci-task notebook")
    if blocksci_task != "notebook" and options.has("--blocksci-notebook-port"):
        errors.append("--blocksci-notebook-port requires --blocksci-task notebook")
    external_bitcoin = _given(options, "--blocksci-external-bitcoin-datadir")
    bitcoin_blocks_uri = _given(options, "--blocksci-bitcoin-blocks-uri")
    external_index = _given(options, "--blocksci-external-blocksci-dir")
    external_network = options.has("--blocksci-network")
    external_max_block = options.has("--blocksci-max-block")
    source_cache_run_id = options.value("--blocksci-cache-source-run-id")
    if blocksci_task == "update":
        if not source_cache_run_id:
            errors.append("--blocksci-task update requires --blocksci-cache-source-run-id")
        if not external_bitcoin:
            errors.append("--blocksci-task update requires --blocksci-external-bitcoin-datadir")
        if external_index:
            errors.append("--blocksci-task update does not support --blocksci-external-blocksci-dir")
        target_run_id = options.value("--run-id")
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
        parse_source = (
            action == "pbs-from-s3"
            and blocksci_workflow == "reusable"
            and blocksci_task == "parse"
        )
        update_source = (
            action == "pbs-from-s3"
            and blocksci_workflow == "cached"
            and blocksci_task == "update"
            and external_bitcoin
            and not external_index
        )
        if not (parse_source or update_source):
            errors.append(
                "external BlockSci sources require either reusable parse or cached update"
            )
    if external_bitcoin or bitcoin_blocks_uri:
        if not external_network or not external_max_block:
            errors.append(
                "an external Bitcoin source requires --blocksci-network and "
                "--blocksci-max-block"
            )
    elif external_network or external_max_block:
        errors.append(
            "--blocksci-network and --blocksci-max-block require "
            "an external Bitcoin source"
        )
    external_baseline_uri = _given(options, "--external-baseline-uri")
    if blocksci_task == "external":
        if action != "pbs-from-s3" or blocksci_workflow == "combined":
            errors.append(
                "--blocksci-task external requires pbs-from-s3 with reusable or cached workflow"
            )
        if options.has("--analysisPbs") or not options.has("--blocksciPbs"):
            errors.append("--blocksci-task external requires --blocksciPbs without --analysisPbs")
        if not external_baseline_uri:
            errors.append("--blocksci-task external requires --external-baseline-uri")
    elif external_baseline_uri:
        errors.append("--external-baseline-uri requires --blocksci-task external")
    if action == "pbs-from-s3":
        for flag in ("--run-id", "--artifact-uri", "--s3-endpoint-url", "--s3-credentials-file", "--s3-profile", "--engine"):
            if not _given(options, flag):
                errors.append(f"pbs-from-s3 requires {flag}")
        if not any(
            options.has(flag)
            for flag in ("--analysisPbs", "--blocksciPbs", "--mappingsPbs")
        ):
            errors.append(
                "pbs-from-s3 requires --analysisPbs, --blocksciPbs, or --mappingsPbs"
            )
        report_resource_flags = (
            "--pbs-unified-report-ncpus",
            "--pbs-unified-report-mem",
            "--pbs-unified-report-scratch",
            "--pbs-unified-report-walltime",
        )
        separate_report = (
            options.has("--blocksciPbs")
            and blocksci_task == "detect"
            and (
                options.has("--analysisPbs")
                or options.has("--mappingsPbs")
                or blocksci_workflow != "combined"
            )
        )
        if any(options.has(flag) for flag in report_resource_flags) and not separate_report:
            errors.append(
                "unified-report PBS resource overrides require a separate unified-report job"
            )
    if backend == "s3" and action == "full-run":
        if blocksci_workflow == "cached":
            errors.append(
                "full-run cannot reuse a cache before emulation; use --blocksci-workflow reusable"
            )
        if options.value("--driver") != "kubernetes":
            errors.append("full-run --artifact-backend s3 requires --driver kubernetes")
        for flag in (
            "--run-id",
            "--artifact-uri",
            "--s3-endpoint-url",
            "--s3-secret-name",
            "--s3-credentials-file",
            "--s3-profile",
        ):
            if not _given(options, flag):
                errors.append(f"full-run --artifact-backend s3 requires {flag}")
        if not options.has("--analysisPbs") or not options.has("--blocksciPbs"):
            errors.append("full-run --artifact-backend s3 requires both --analysisPbs and --blocksciPbs")
        if not options.has("--reuse-namespace"):
            errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created"
            )
        if options.has("--parallel"):
            errors.append("full-run --artifact-backend s3 does not support --parallel")
        if options.has("--blocksci-script") or options.has("--blocksciScript"):
            errors.append("full-run --artifact-backend s3 does not support --blocksci-script")
        for flag in ("--kubernetes-btc-datadir", "--pbs-bitcoin-datadir", "--copy-to-host"):
            if options.has(flag):
                errors.append(f"Kubernetes S3-compatible mode does not support {flag}")
    if backend == "s3" and action == "emulate":
        for flag in (
            "--run-id",
            "--artifact-uri",
            "--s3-endpoint-url",
            "--s3-secret-name",
            "--s3-credentials-file",
            "--s3-profile",
        ):
            if not _given(options, flag):
                errors.append(f"Kubernetes S3-compatible mode requires {flag}")
        if options.value("--driver") != "kubernetes":
            errors.append("--artifact-backend s3 requires --driver kubernetes")
        if not options.has("--reuse-namespace"):
            errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created"
            )
        for flag in ("--kubernetes-btc-datadir", "--pbs-bitcoin-datadir", "--copy-to-host"):
            if options.has(flag):
                errors.append(f"Kubernetes S3-compatible mode does not support {flag}")
    if action == "full-run" and backend != "s3" and (
        blocksci_workflow != "combined" or blocksci_task != "detect"
    ):
        errors.append(
            "reusable BlockSci workflows are currently supported only with the S3 artifact backend"
        )
    engine = options.value("--engine")
    if engine is not None and engine not in {"wasabi", "joinmarket"}:
        errors.append("--engine must be wasabi or joinmarket")
    script = options.value("--blocksci-script") or options.value("--blocksciScript")
    if script and not Path(script).expanduser().is_file():
        errors.append(f"BlockSci script not found: {script}")
    mappings_pbs = options.has("--mappingsPbs")
    if mappings_pbs and options.value("--engine") not in (None, "wasabi"):
        errors.append("--mappingsPbs is supported only with --engine wasabi")
    if mappings_pbs and (options.value("--coinjoin-type") or "wasabi2") != "wasabi2":
        errors.append("--mappingsPbs requires --coinjoin-type wasabi2")
    if action == "mappings" and not mappings_pbs:
        errors.append("mappings requires --mappingsPbs")
    return errors
