#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "questionary>=2.1,<3",
#   "rich>=13.9,<15",
# ]
# ///
"""Interactive command builder for coinjoin-pipeline."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import os
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Iterable

from .command_metadata import (
    all_option_metadata,
    command_metadata,
    option_metadata,
    parser_flags,
    takes_value,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 without zoneinfo in the stdlib
    ZoneInfo = None  # type: ignore[assignment,misc]


_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class Command:
    action: str
    runtime: str = "docker"
    version: str | None = None
    options: list[tuple[str, str | None]] = field(default_factory=list)
    env: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


KUBERNETES_FLAGS = {
    "--driver", "--namespace", "--reuse-namespace", "--kubeconfig", "--image-prefix",
    "--kubernetes-btc-datadir", "--copy-to-host", "--coinjoin-infrastructure-local-build",
}
PBS_FLAGS = {
    "--analysisPbs", "--blocksciPbs", "--mappingsPbs", "--pbs-ncpus", "--pbs-mem", "--pbs-scratch",
    "--pbs-walltime", "--pbs-image", "--pbs-blocksci-image",
    "--pbs-coinjoin-analysis-image", "--pbs-bitcoin-datadir",
    "--pbs-unified-report-ncpus", "--pbs-unified-report-mem",
    "--pbs-unified-report-scratch", "--pbs-unified-report-walltime",
    "--pbs-mappings-enumerator-image", "--pbs-sake-image",
    "--mapping-mining-fee-rate", "--mapping-coordination-fee-rate",
    "--mapping-max-decomposition-fee", "--mapping-mode", "--mapping-timeout",
    "--mapping-retry-timeout", "--sake-seed",
    "--blocksci-workflow", "--blocksci-task", "--blocksci-notebook-port",
    "--blocksci-notebooks-dir", "--blocksci-external-bitcoin-datadir",
    "--blocksci-external-blocksci-dir", "--blocksci-network",
    "--blocksci-max-block", "--blocksci-cache-source-run-id",
    *{
        f"--pbs-{stage}-{resource}"
        for stage in ("analysis", "blocksci", "mappings")
        for resource in ("ncpus", "mem", "scratch", "walltime")
    },
}
# Flags that only affect the BlockSci PBS stage (--blocksciPbs). The shared
# Bitcoin datadir is BlockSci-specific: coinjoin-analysis PBS reads emulator
# artifacts from the run directory and never touches the Bitcoin datadir.
PBS_BLOCKSCI_ONLY = {
    "--blocksciPbs", "--pbs-blocksci-image", "--pbs-bitcoin-datadir",
    "--blocksci-workflow", "--blocksci-task", "--blocksci-notebook-port",
    "--blocksci-notebooks-dir", "--blocksci-external-bitcoin-datadir",
    "--blocksci-external-blocksci-dir", "--blocksci-network",
    "--blocksci-max-block", "--blocksci-cache-source-run-id",
    *{
        f"--pbs-blocksci-{resource}"
        for resource in ("ncpus", "mem", "scratch", "walltime")
    },
}
# Flags that only affect the coinjoin-analysis PBS stage (--analysisPbs).
PBS_ANALYSIS_ONLY = {
    "--analysisPbs", "--pbs-coinjoin-analysis-image",
    *{
        f"--pbs-analysis-{resource}"
        for resource in ("ncpus", "mem", "scratch", "walltime")
    },
}
PBS_REPORT_ONLY = {
    "--pbs-unified-report-ncpus", "--pbs-unified-report-mem",
    "--pbs-unified-report-scratch", "--pbs-unified-report-walltime",
}
PBS_MAPPINGS_ONLY = {"--mappingsPbs", "--pbs-mappings-enumerator-image", "--pbs-sake-image",
                     "--mapping-mining-fee-rate", "--mapping-coordination-fee-rate",
                     "--mapping-max-decomposition-fee", "--mapping-mode", "--mapping-timeout",
                     "--mapping-retry-timeout", "--sake-seed",
                     *{f"--pbs-mappings-{resource}"
                       for resource in ("ncpus", "mem", "scratch", "walltime")}}
# Flags meaningful for either PBS stage (resources and the shared image override).
PBS_SHARED = PBS_FLAGS - PBS_BLOCKSCI_ONLY - PBS_ANALYSIS_ONLY - PBS_REPORT_ONLY - PBS_MAPPINGS_ONLY
SEMANTICALLY_DISABLED_FLAGS = {
    "analyze": {"--analysisPbs", "--pbs-coinjoin-analysis-image"},
    "coinjoin-analysis": PBS_BLOCKSCI_ONLY,
}


def base_action(command: Command) -> str:
    if command.action.startswith(("runs ", "scenarios ", "external ")):
        return " ".join(command.action.split()[:2])
    return command.action


def supported_flags(action: str) -> set[str]:
    return parser_flags(action) - SEMANTICALLY_DISABLED_FLAGS.get(action, set())


def option_values(command: Command, flag: str) -> list[str | None]:
    return [value for name, value in command.options if name == flag]


def option_value(command: Command, flag: str) -> str | None:
    values = option_values(command, flag)
    return values[-1] if values else None


def has_option(command: Command, flag: str) -> bool:
    return any(name == flag for name, _ in command.options)


def parse_command(source: str) -> Command:
    """Parse a previously generated or hand-written runIt.sh command."""
    tokens = shlex.split(source.replace("\\\n", " "))
    # Preserve leading KEY=value environment assignments (e.g. the documented
    # Podman CONTAINER_SOCKET override) that precede ./runIt.sh.
    env: list[tuple[str, str]] = []
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        key, value = tokens.pop(0).split("=", 1)
        env.append((key, value))
    script_index = next(
        (
            index for index, token in enumerate(tokens)
            if Path(token).name in {"runIt.sh", "coinjoin-pipeline", "cjp"}
        ),
        None,
    )
    if script_index is None:
        raise ValueError("Command must contain coinjoin-pipeline, cjp, or runIt.sh.")
    tokens = tokens[script_index + 1 :]
    runtime = "docker"
    version = None
    while len(tokens) >= 2 and tokens[0] in {"--runtime", "--version"}:
        flag, value = tokens[:2]
        tokens = tokens[2:]
        if flag == "--runtime":
            runtime = value
        else:
            version = value
    if len(tokens) >= 2 and tokens[0] == "container" and tokens[1] in {"docker", "podman"}:
        runtime = tokens[1]
        tokens = tokens[2:]
    if not tokens or tokens[0].startswith("--"):
        action = "full-run"
    elif tokens[0] in {"runs", "scenarios"}:
        if len(tokens) < 2:
            raise ValueError(f"{tokens[0]} requires a subcommand.")
        action = " ".join(tokens[:2])
        tokens = tokens[2:]
        if action in {"scenarios show", "scenarios validate"} and tokens and not tokens[0].startswith("-"):
            action += f" {shell_quote(tokens.pop(0))}"
    elif tokens[0] == "external":
        if len(tokens) < 2:
            raise ValueError("external requires a subcommand.")
        action = " ".join(tokens[:2])
        tokens = tokens[2:]
    else:
        action = "coinjoin-analysis" if tokens[0] == "coinjoin" else tokens.pop(0)

    options: list[tuple[str, str | None]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            raise ValueError(f"Unexpected positional argument: {token}")
        if "=" in token:
            flag, value = token.split("=", 1)
            options.append(("--blocksci-script" if flag == "--blocksciScript" else flag, value))
            index += 1
            continue
        flag = "--blocksci-script" if token == "--blocksciScript" else token
        if takes_value(flag):
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise ValueError(f"{flag} requires a value.")
            options.append((flag, tokens[index + 1]))
            index += 2
        else:
            options.append((flag, None))
            index += 1
    return Command(action=action, runtime=runtime, version=version, options=options, env=env)


def validate_command(command: Command) -> Validation:
    """Check cross-option constraints before a command is shown or executed."""
    result = Validation()
    action = base_action(command)
    engine = option_value(command, "--engine")
    driver = option_value(command, "--driver") or "docker"

    if command.runtime not in {"docker", "podman"}:
        result.errors.append("Runtime must be docker or podman.")
    if action not in command_metadata():
        result.errors.append(f"Unsupported action: {command.action}")
    else:
        invalid_flags = sorted({flag for flag, _ in command.options} - supported_flags(action))
        if invalid_flags:
            result.errors.append(
                f"{action} does not support: {', '.join(invalid_flags)}."
            )
    for flag, value in command.options:
        metadata = option_metadata(action, flag)
        if metadata is not None and metadata.choices and value not in metadata.choices:
            result.errors.append(
                f"{flag} must be one of: {', '.join(metadata.choices)}."
            )
    action_metadata = command_metadata().get(action)
    required_flags = (
        {metadata.flag for metadata in action_metadata.options.values() if metadata.required}
        if action_metadata is not None
        else set()
    )
    for flag in sorted(required_flags):
        if not has_option(command, flag):
            result.errors.append(f"{action} requires {flag}.")
    if action in {"analyze", "export", "mappings"} and not has_option(command, "--run-dir"):
        result.errors.append(f"{action} requires --run-dir.")
    if action in {"runs inspect", "runs validate"} and not has_option(command, "--run-dir"):
        result.errors.append(f"{action} requires --run-dir.")
    if action in {"scenarios show", "scenarios validate"}:
        if len(command.action.split()) < 3:
            result.errors.append(f"{action} requires a scenario name or path.")
    if action == "coinjoin-analysis":
        targets = sum(has_option(command, flag) for flag in ("--run-dir", "--all-runs"))
        if targets != 1:
            result.errors.append("coinjoin-analysis requires exactly one of --run-dir or --all-runs.")
        if has_option(command, "--all-runs") and has_option(command, "--analysisPbs"):
            result.errors.append(
                "--all-runs cannot be combined with --analysisPbs; the PBS coinjoin-analysis "
                "stage targets a single run directory."
            )
    if action == "clean" and not (has_option(command, "--yes") or has_option(command, "--dry-run")):
        result.errors.append("clean requires --yes or --dry-run.")
    if has_option(command, "--yes") and has_option(command, "--dry-run"):
        result.errors.append("Use either --yes or --dry-run for clean, not both.")

    if driver == "kubernetes" and action not in {"full-run", "recreate"}:
        result.errors.append("--driver kubernetes is supported only by full-run and recreate.")
    driver_dependent_flags = KUBERNETES_FLAGS - {"--driver", "--coinjoin-infrastructure-local-build"}
    if driver != "kubernetes" and any(has_option(command, flag) for flag in driver_dependent_flags):
        result.errors.append("Kubernetes-specific options require --driver kubernetes.")
    if has_option(command, "--copy-to-host") and has_option(command, "--kubernetes-btc-datadir"):
        result.errors.append("--copy-to-host cannot be combined with --kubernetes-btc-datadir.")

    analysis_pbs = has_option(command, "--analysisPbs")
    blocksci_pbs = has_option(command, "--blocksciPbs")
    mappings_pbs = has_option(command, "--mappingsPbs")
    if analysis_pbs and action not in {"full-run", "coinjoin-analysis", "pbs-from-s3"}:
        result.errors.append(
            "--analysisPbs is supported only by full-run and coinjoin-analysis "
            "(or pbs-from-s3)."
        )
    if blocksci_pbs and action not in {"full-run", "analyze", "pbs-from-s3"}:
        result.errors.append(
            "--blocksciPbs is supported only by full-run and analyze (or pbs-from-s3)."
        )
    if mappings_pbs and action not in {"full-run", "mappings", "pbs-from-s3"}:
        result.errors.append(
            "--mappingsPbs is supported only by full-run, mappings, and pbs-from-s3."
        )
    if mappings_pbs and engine != "wasabi":
        result.errors.append("--mappingsPbs requires --engine wasabi.")
    if mappings_pbs and (option_value(command, "--coinjoin-type") or "wasabi2") != "wasabi2":
        result.errors.append("--mappingsPbs requires --coinjoin-type wasabi2.")
    backend = "s3" if action == "pbs-from-s3" else (option_value(command, "--artifact-backend") or "shared-storage")
    blocksci_workflow = option_value(command, "--blocksci-workflow") or "combined"
    blocksci_task = option_value(command, "--blocksci-task") or "detect"
    if blocksci_pbs and backend != "s3" and not has_option(command, "--pbs-bitcoin-datadir"):
        result.errors.append("--blocksciPbs requires --pbs-bitcoin-datadir.")
    if has_option(command, "--pbs-bitcoin-datadir") and not blocksci_pbs:
        result.errors.append("--pbs-bitcoin-datadir requires --blocksciPbs.")
    if backend == "s3" and action == "full-run":
        if driver != "kubernetes":
            result.errors.append("full-run --artifact-backend s3 requires --driver kubernetes.")
        for flag in (
            "--run-id",
            "--artifact-uri",
            "--s3-endpoint-url",
            "--s3-secret-name",
            "--s3-credentials-file",
            "--s3-profile",
        ):
            if not has_option(command, flag):
                result.errors.append(f"full-run --artifact-backend s3 requires {flag}.")
        if not analysis_pbs or not blocksci_pbs:
            result.errors.append("full-run --artifact-backend s3 requires both --analysisPbs and --blocksciPbs.")
        if not has_option(command, "--reuse-namespace"):
            result.errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created."
            )
        if has_option(command, "--parallel"):
            result.errors.append("full-run --artifact-backend s3 does not support --parallel.")
        if has_option(command, "--blocksci-script"):
            result.errors.append("full-run --artifact-backend s3 does not support --blocksci-script.")
        for flag in ("--kubernetes-btc-datadir", "--pbs-bitcoin-datadir", "--copy-to-host"):
            if has_option(command, flag):
                result.errors.append(f"Kubernetes S3-compatible mode does not support {flag}.")
    if backend == "s3" and action == "recreate":
        if driver != "kubernetes":
            result.errors.append("--artifact-backend s3 requires --driver kubernetes.")
        for flag in ("--run-id", "--artifact-uri", "--s3-endpoint-url", "--s3-secret-name"):
            if not has_option(command, flag):
                result.errors.append(f"Kubernetes S3-compatible mode requires {flag}.")
        if not has_option(command, "--reuse-namespace"):
            result.errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created."
            )
        for flag in ("--kubernetes-btc-datadir", "--pbs-bitcoin-datadir", "--copy-to-host"):
            if has_option(command, flag):
                result.errors.append(f"Kubernetes S3-compatible mode does not support {flag}.")
    if action == "pbs-from-s3":
        for flag in (
            "--run-id", "--artifact-uri", "--s3-endpoint-url",
            "--s3-credentials-file", "--s3-profile", "--engine",
        ):
            if not has_option(command, flag):
                result.errors.append(f"pbs-from-s3 requires {flag}.")
        if not analysis_pbs and not blocksci_pbs and not mappings_pbs:
            result.errors.append(
                "pbs-from-s3 requires --analysisPbs, --blocksciPbs, or --mappingsPbs."
            )
    if blocksci_workflow != "combined" and not blocksci_pbs:
        result.errors.append("Reusable BlockSci workflows require --blocksciPbs.")
    if blocksci_task == "parse":
        if action != "pbs-from-s3" or blocksci_workflow != "reusable":
            result.errors.append("--blocksci-task parse requires pbs-from-s3 --blocksci-workflow reusable.")
        if analysis_pbs or not blocksci_pbs:
            result.errors.append("--blocksci-task parse requires --blocksciPbs without --analysisPbs.")
    elif blocksci_task == "update":
        if action != "pbs-from-s3" or blocksci_workflow != "cached":
            result.errors.append(
                "--blocksci-task update requires pbs-from-s3 --blocksci-workflow cached."
            )
        if analysis_pbs or not blocksci_pbs:
            result.errors.append("--blocksci-task update requires --blocksciPbs without --analysisPbs.")
    elif blocksci_task in {"script", "notebook"}:
        if action != "pbs-from-s3" or blocksci_workflow == "combined":
            result.errors.append(
                "BlockSci script and notebook tasks require pbs-from-s3 "
                "--blocksci-workflow reusable or cached."
            )
        if analysis_pbs or not blocksci_pbs:
            result.errors.append(
                "BlockSci script and notebook tasks require --blocksciPbs without --analysisPbs."
            )
    if action == "pbs-from-s3" and blocksci_task == "script" and not has_option(command, "--blocksci-script"):
        result.errors.append("--blocksci-task script requires --blocksci-script.")
    if action == "pbs-from-s3" and blocksci_task != "script" and has_option(command, "--blocksci-script"):
        result.errors.append("--blocksci-script requires --blocksci-task script.")
    if blocksci_task != "notebook" and has_option(command, "--blocksci-notebooks-dir"):
        result.errors.append("--blocksci-notebooks-dir requires --blocksci-task notebook.")
    if blocksci_task != "notebook" and has_option(command, "--blocksci-notebook-port"):
        result.errors.append("--blocksci-notebook-port requires --blocksci-task notebook.")
    external_bitcoin = has_option(command, "--blocksci-external-bitcoin-datadir")
    external_index = has_option(command, "--blocksci-external-blocksci-dir")
    external_network = has_option(command, "--blocksci-network")
    external_max_block = has_option(command, "--blocksci-max-block")
    source_cache_run_id = option_value(command, "--blocksci-cache-source-run-id")
    if blocksci_task == "update":
        if not source_cache_run_id:
            result.errors.append("--blocksci-task update requires --blocksci-cache-source-run-id.")
        if not external_bitcoin:
            result.errors.append("--blocksci-task update requires --blocksci-external-bitcoin-datadir.")
        if external_index:
            result.errors.append("--blocksci-task update does not support --blocksci-external-blocksci-dir.")
        if source_cache_run_id and source_cache_run_id == option_value(command, "--run-id"):
            result.errors.append("--blocksci-cache-source-run-id must differ from target --run-id.")
    elif source_cache_run_id:
        result.errors.append("--blocksci-cache-source-run-id requires --blocksci-task update.")
    if external_bitcoin and external_index:
        result.errors.append(
            "Choose either --blocksci-external-bitcoin-datadir or "
            "--blocksci-external-blocksci-dir, not both."
        )
    if external_bitcoin or external_index:
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
            result.errors.append(
                "External BlockSci sources require either reusable parse or cached update."
            )
    if external_bitcoin:
        if not external_network or not external_max_block:
            result.errors.append(
                "--blocksci-external-bitcoin-datadir requires --blocksci-network "
                "and --blocksci-max-block."
            )
    elif external_network or external_max_block:
        result.errors.append(
            "--blocksci-network and --blocksci-max-block require "
            "--blocksci-external-bitcoin-datadir."
        )
    # Shared PBS resources (ncpus/mem/scratch/walltime/shared image) require at
    # least one PBS stage. Stage-specific images require their own stage flag.
    if not (analysis_pbs or blocksci_pbs or mappings_pbs) and any(has_option(command, flag) for flag in PBS_SHARED):
        result.errors.append("PBS resource/image options require --analysisPbs, --blocksciPbs, or --mappingsPbs.")
    if has_option(command, "--pbs-blocksci-image") and not blocksci_pbs:
        result.errors.append("--pbs-blocksci-image requires --blocksciPbs.")
    if has_option(command, "--pbs-coinjoin-analysis-image") and not analysis_pbs:
        result.errors.append("--pbs-coinjoin-analysis-image requires --analysisPbs.")
    for stage, enabled, enabling_flag in (
        ("analysis", analysis_pbs, "--analysisPbs"),
        ("blocksci", blocksci_pbs, "--blocksciPbs"),
        ("mappings", mappings_pbs, "--mappingsPbs"),
    ):
        stage_resources = (
            f"--pbs-{stage}-{resource}"
            for resource in ("ncpus", "mem", "scratch", "walltime")
        )
        if not enabled and any(has_option(command, flag) for flag in stage_resources):
            result.errors.append(
                f"{stage}-specific PBS resources require {enabling_flag}."
            )
    for flag in sorted(PBS_REPORT_ONLY):
        separate_report = blocksci_pbs and blocksci_task == "detect" and (
            analysis_pbs or mappings_pbs or blocksci_workflow != "combined"
        )
        if has_option(command, flag) and not separate_report:
            result.errors.append(f"{flag} requires a separate unified-report job.")
    for flag in sorted(PBS_MAPPINGS_ONLY - {"--mappingsPbs"}):
        if has_option(command, flag) and not mappings_pbs:
            result.errors.append(f"{flag} requires --mappingsPbs.")

    kube_datadir = option_value(command, "--kubernetes-btc-datadir")
    pbs_datadir = option_value(command, "--pbs-bitcoin-datadir")
    if driver == "kubernetes" and blocksci_pbs and not has_option(command, "--copy-to-host"):
        if kube_datadir and pbs_datadir and Path(kube_datadir).expanduser() != Path(pbs_datadir).expanduser():
            result.errors.append(
                "Direct Kubernetes+PBS storage requires --kubernetes-btc-datadir and "
                "--pbs-bitcoin-datadir to be identical."
            )
        if pbs_datadir and not pbs_datadir.startswith("/storage/"):
            result.warnings.append("Verify the PBS datadir is shared by Kubernetes workers and PBS nodes.")
    if pbs_datadir and "<" in pbs_datadir:
        result.warnings.append("Replace placeholders in --pbs-bitcoin-datadir before running.")
    if action == "full-run" and not has_option(command, "--dry-run"):
        result.warnings.append("full-run begins by removing pipeline runtime containers and volumes.")
    if driver == "kubernetes" and blocksci_pbs and has_option(command, "--copy-to-host"):
        result.warnings.append(
            "With --copy-to-host, the PBS datadir must already receive the copied regtest blocks."
        )

    joinmarket_flags = {
        "--joinmarket-detector", "--joinmarket-min-base-fee",
        "--joinmarket-percentage-fee", "--joinmarket-max-depth",
    }
    if engine != "joinmarket" and any(has_option(command, flag) for flag in joinmarket_flags):
        result.warnings.append("JoinMarket detector options have no effect with the Wasabi engine.")
    integer_flags = (
        "--min-input-count", "--joinmarket-min-base-fee", "--joinmarket-max-depth",
        "--pbs-ncpus", "--min-free-gb", "--mapping-mining-fee-rate",
        "--mapping-max-decomposition-fee", "--mapping-timeout", "--mapping-retry-timeout",
        "--sake-seed",
    )
    for flag in integer_flags:
        value = option_value(command, flag)
        if value is not None:
            try:
                valid_integer = int(value) >= 0
            except ValueError:
                valid_integer = False
            if not valid_integer:
                result.errors.append(f"{flag} must be a non-negative integer.")
    for flag in ("--mapping-timeout", "--mapping-retry-timeout"):
        value = option_value(command, flag)
        if value is not None and value.isdigit() and int(value) == 0:
            result.errors.append(f"{flag} must be greater than zero.")
    coordination_fee = option_value(command, "--mapping-coordination-fee-rate")
    if coordination_fee is not None:
        try:
            valid_coordination_fee = float(coordination_fee) >= 0
        except ValueError:
            valid_coordination_fee = False
        if not valid_coordination_fee:
            result.errors.append("--mapping-coordination-fee-rate must be a non-negative number.")
    percentage_fee = option_value(command, "--joinmarket-percentage-fee")
    if percentage_fee is not None:
        try:
            valid_percentage = float(percentage_fee) >= 0
        except ValueError:
            valid_percentage = False
        if not valid_percentage:
            result.errors.append("--joinmarket-percentage-fee must be a non-negative number.")
    run_timezone_value = option_value(command, "--run-timezone")
    if run_timezone_value is not None:
        if ZoneInfo is None:
            result.warnings.append("Cannot validate --run-timezone: zoneinfo is unavailable.")
        else:
            try:
                ZoneInfo(run_timezone_value)
            except Exception:
                result.errors.append(
                    f"--run-timezone must be a valid IANA timezone: {run_timezone_value}"
                )
    blocksci_script = option_value(command, "--blocksci-script")
    if blocksci_script is not None and not Path(blocksci_script).expanduser().is_file():
        result.errors.append(f"--blocksci-script not found or not a file: {blocksci_script}")
    if action == "external analyze":
        resume = has_option(command, "--resume")
        supplied_inputs = any(has_option(command, flag) for flag in ("--bitcoin-datadir", "--baseline"))
        if resume and supplied_inputs:
            result.errors.append("--resume cannot be combined with --bitcoin-datadir or --baseline.")
        if not resume and not all(has_option(command, flag) for flag in ("--bitcoin-datadir", "--baseline")):
            result.errors.append("A new external run requires --bitcoin-datadir and --baseline.")
    return result


def explain_command(command: Command) -> list[str]:
    """Return a compact operational summary of the selected command."""
    action = command.action.split()[0]
    descriptions = {
        "full-run": "clean runtime resources, emulate CoinJoins, analyze, and export a report",
        "recreate": "run CoinJoin emulation only",
        "analyze": "analyze an existing emulator run",
        "export": "regenerate the unified report from existing analysis outputs",
        "coinjoin-analysis": "run the baseline coinjoin-analysis stage only",
        "mappings": "run the Wasabi mapping enumerator and Sake PBS stage",
        "initialize": "prefetch required container images",
        "clean": "remove runtime containers, networks, and volumes",
        "runs": "inspect the preserved run catalog",
        "scenarios": "inspect or validate scenario definitions",
        "external": "build a persistent BlockSci report for external blockchain data",
    }
    lines = [
        f"Action: {command.action}",
        f"Workflow: {descriptions.get(action, 'run the selected command')}",
        f"Local runtime: {command.runtime}",
    ]
    engine = option_value(command, "--engine")
    if engine:
        lines.append(f"CoinJoin engine: {engine}")
    if option_value(command, "--driver") == "kubernetes":
        lines.append(f"Emulation: Kubernetes namespace {option_value(command, '--namespace') or 'coinjoin'}")
    stages = []
    if has_option(command, "--analysisPbs"):
        stages.append("coinjoin-analysis")
    if has_option(command, "--blocksciPbs"):
        stages.append("BlockSci")
    if has_option(command, "--mappingsPbs"):
        stages.append("CoinJoin mappings + Sake")
    if stages:
        lines.append(f"PBS stages: {', '.join(stages)}")
    if has_option(command, "--parallel"):
        lines.append("Analysis scheduling: parallel")
    target = option_value(command, "--run-dir") or option_value(command, "--run-id")
    if target:
        lines.append(f"Target run: {target}")
    if has_option(command, "--dry-run"):
        lines.append("Safety: dry-run; pipeline work will not be launched")
    elif action == "clean":
        lines.append("Safety: confirmed destructive runtime cleanup")
    else:
        lines.append("Safety: command will perform pipeline work when executed")
    return lines


def shell_quote(value: str) -> str:
    """Quote a value while keeping the documented HOME expression readable."""
    if value == "${HOME}/.kube/config":
        return '"${HOME}/.kube/config"'
    return shlex.quote(value)


def render_command(command: Command) -> str:
    """Render a copy/paste friendly, multiline runIt.sh command."""
    head = ["coinjoin-pipeline", "--runtime", command.runtime]
    if command.version:
        head.extend(("--version", shell_quote(command.version)))
    head.append(command.action)
    parts = [" ".join(head)]
    for flag, value in command.options:
        suffix = flag if value is None else f"{flag} {shell_quote(value)}"
        parts.append(f"  {suffix}")
    rendered = " \\\n".join(parts)
    if command.env:
        prefix = " ".join(f"{key}={shell_quote(value)}" for key, value in command.env)
        rendered = f"{prefix} {rendered}"
    return rendered


def command_argv(command: Command) -> list[str]:
    """Return an unquoted argv representation suitable for safe execution."""
    argv = ["coinjoin-pipeline", "--runtime", command.runtime]
    if command.version:
        argv.extend(("--version", command.version))
    argv.extend(shlex.split(command.action))
    for flag, value in command.options:
        argv.append(flag)
        if value is not None:
            argv.append(value)
    return argv


def preflight_command(command: Command) -> Command:
    """Clone a command and force its non-mutating dry-run mode."""
    action = base_action(command)
    if "--dry-run" not in parser_flags(action):
        raise ValueError(f"{action} does not support --dry-run preflight.")
    options = [
        (flag, value)
        for flag, value in command.options
        if flag not in {"--dry-run", "--yes"}
    ]
    options.append(("--dry-run", None))
    return Command(
        action=command.action,
        runtime=command.runtime,
        version=command.version,
        options=options,
        env=list(command.env),
    )


def run_preflight(command: Command) -> int:
    """Execute the generated command in its documented dry-run mode."""
    preflight = preflight_command(command)
    environment = os.environ.copy()
    environment.update(dict(preflight.env))
    return subprocess.run(
        command_argv(preflight),
        cwd=Path(__file__).resolve().parent,
        env=environment,
        check=False,
    ).returncode


def metadata_choices(action: str, flag: str, fallback: Iterable[str] = ()) -> list[str]:
    metadata = option_metadata(action, flag)
    return list(metadata.choices) if metadata is not None and metadata.choices else list(fallback)


def metadata_default(action: str, flag: str, fallback: str = "") -> str:
    metadata = option_metadata(action, flag)
    if metadata is None or metadata.default_text() is None:
        return fallback
    return metadata.default_text() or fallback


def contextual_help(action: str, flag: str) -> str:
    """Format parser-owned help and default text for an interactive choice."""
    metadata = option_metadata(action, flag)
    if metadata is None:
        return ""
    details = metadata.help.strip().rstrip(".")
    default = metadata.default_text()
    if default and "default:" not in details.lower():
        details = f"{details}; default: {default}" if details else f"default: {default}"
    return details


def completion_values(flag: str) -> list[str]:
    """Return local and parser-derived values for interactive completion."""
    project_dir = Path(__file__).resolve().parent
    values: set[str] = set()
    if flag == "--scenario":
        scenario_root = files("coinjoin_pipeline").joinpath("resources/scenarios")
        values.update(item.name for item in scenario_root.iterdir() if item.name.endswith(".json"))
    elif flag in {"--run-dir", "--run-id"}:
        runs_root = Path(os.environ.get("EMULATION_LOGS_DIR", project_dir / "emulation_logs"))
        if runs_root.is_dir():
            values.update(path.name for path in runs_root.iterdir() if path.is_dir())
    elif flag == "--kubeconfig":
        values.add("${HOME}/.kube/config")
        values.add(str(Path.home() / ".kube" / "config"))
    elif "image" in flag:
        for metadata in all_option_metadata(flag):
            default = metadata.default_text()
            if default:
                values.add(default)
            help_default = re.search(r"default:\s*([^.)]+(?:\.[^.)]+)*)\)", metadata.help)
            if help_default:
                values.add(help_default.group(1).strip())
        for variable in (
            "WRAPPER_IMAGE", "BLOCKSCI_IMAGE", "COINJOIN_ANALYSIS_IMAGE",
            "COINJOIN_EMULATOR_IMAGE",
        ):
            if os.environ.get(variable):
                values.add(os.environ[variable])
    return sorted(values)


def _nonempty(value: str) -> bool | str:
    return True if value.strip() else "Enter a value."


def collect_command() -> Command:
    """Prompt for the complete runIt.sh command surface."""
    import questionary
    from questionary import Choice

    style = questionary.Style(
        [
            ("qmark", "fg:#00d7af bold"),
            ("question", "bold"),
            ("answer", "fg:#00d7af bold"),
            ("pointer", "fg:#00d7af bold"),
            ("highlighted", "fg:#00d7af bold"),
        ]
    )

    def select(message: str, choices: Iterable[str | Choice], default: str | None = None) -> str:
        answer = questionary.select(message, choices=list(choices), default=default, style=style).ask()
        if answer is None:
            raise KeyboardInterrupt
        return answer

    def confirm(message: str, default: bool = False) -> bool:
        answer = questionary.confirm(message, default=default, style=style).ask()
        if answer is None:
            raise KeyboardInterrupt
        return bool(answer)

    def checkbox(message: str, choices: Iterable[Choice]) -> set[str]:
        answer = questionary.checkbox(message, choices=list(choices), style=style).ask()
        if answer is None:
            raise KeyboardInterrupt
        return set(answer)

    def text(message: str, default: str = "", validate=None, flag: str | None = None) -> str:
        path_flags = {
            "--blocksci-script", "--kubeconfig", "--bitcoin-datadir", "--baseline",
            "--false-cjtxs", "--kubernetes-btc-datadir", "--pbs-bitcoin-datadir",
        }
        suggestions = completion_values(flag) if flag else []
        if flag in path_flags:
            prompt = questionary.path(message, default=default, validate=validate, style=style)
        elif suggestions:
            prompt = questionary.autocomplete(
                message,
                choices=suggestions,
                default=default,
                validate=validate,
                match_middle=True,
                ignore_case=True,
                style=style,
            )
        else:
            prompt = questionary.text(message, default=default, validate=validate, style=style)
        answer = prompt.ask()
        if answer is None:
            raise KeyboardInterrupt
        return answer.strip()

    def add_flag(result: Command, flag: str, enabled: bool) -> None:
        if enabled:
            result.options.append((flag, None))

    def add_value(result: Command, flag: str, message: str, default: str = "") -> None:
        action = base_action(result)
        resolved_default = metadata_default(action, flag, default)
        result.options.append((flag, text(message, resolved_default, _nonempty, flag)))

    def advanced_choice(result: Command, label: str, flag: str, value: str | None = None) -> Choice:
        hint = contextual_help(base_action(result), flag)
        title = f"{label} — {hint}" if hint else label
        return Choice(title, value or flag.removeprefix("--"))

    def choose_scenario(result: Command) -> None:
        scenarios = sorted(path.name for path in (Path(__file__).parent / "scenarios").glob("*.json"))
        choices = [Choice("Use default / run manifest", ""), *scenarios, Choice("Enter another path", "__custom__")]
        scenario = select("Scenario", choices, "")
        if scenario == "__custom__":
            scenario = text("Scenario path", validate=_nonempty, flag="--scenario")
        if scenario:
            result.options.append(("--scenario", scenario))

    def choose_pbs(result: Command, *, allow_analysis: bool, allow_blocksci: bool,
                   allow_mappings: bool = False) -> None:
        stages: list[Choice] = []
        if allow_analysis:
            stages.append(Choice("coinjoin-analysis on PBS", "analysis"))
        if allow_blocksci:
            stages.append(Choice("BlockSci on PBS", "blocksci"))
        if allow_mappings:
            stages.append(Choice("mapping enumerator + Sake on PBS", "mappings"))
        selected = checkbox("PBS stages", stages)
        add_flag(result, "--analysisPbs", "analysis" in selected)
        add_flag(result, "--blocksciPbs", "blocksci" in selected)
        add_flag(result, "--mappingsPbs", "mappings" in selected)
        if "blocksci" in selected:
            add_value(
                result,
                "--pbs-bitcoin-datadir",
                "Shared Bitcoin datadir",
                "/storage/<site>/<user>/bitcoin-regtest-data",
            )
        if not selected or not confirm("Configure advanced PBS resources/images?"):
            return
        advanced_choices = [
            advanced_choice(result, "CPU count", "--pbs-ncpus", "--pbs-ncpus"),
            advanced_choice(result, "Memory", "--pbs-mem", "--pbs-mem"),
            advanced_choice(result, "Scratch storage", "--pbs-scratch", "--pbs-scratch"),
            advanced_choice(result, "Walltime", "--pbs-walltime", "--pbs-walltime"),
            advanced_choice(result, "Shared Singularity image", "--pbs-image", "--pbs-image"),
        ]
        if allow_blocksci:
            advanced_choices.append(
                advanced_choice(
                    result,
                    "BlockSci Singularity image",
                    "--pbs-blocksci-image",
                    "--pbs-blocksci-image",
                )
            )
        if allow_analysis:
            advanced_choices.append(
                advanced_choice(
                    result,
                    "coinjoin-analysis Singularity image",
                    "--pbs-coinjoin-analysis-image",
                    "--pbs-coinjoin-analysis-image",
                )
            )
        if allow_mappings:
            advanced_choices.extend([
                advanced_choice(result, "Mappings enumerator image", "--pbs-mappings-enumerator-image"),
                advanced_choice(result, "Sake image", "--pbs-sake-image"),
                advanced_choice(result, "Mapping mining fee rate", "--mapping-mining-fee-rate"),
                advanced_choice(result, "Mapping coordination fee rate", "--mapping-coordination-fee-rate"),
                advanced_choice(result, "Maximum decomposition fee", "--mapping-max-decomposition-fee"),
                advanced_choice(result, "Mapping mode", "--mapping-mode"),
                advanced_choice(result, "Mapping timeout", "--mapping-timeout"),
                advanced_choice(result, "Mapping retry timeout", "--mapping-retry-timeout"),
                advanced_choice(result, "Sake random seed", "--sake-seed"),
            ])
        advanced = checkbox("Advanced PBS options", advanced_choices)
        prompts = {
            "--pbs-ncpus": ("PBS CPU count", ""),
            "--pbs-mem": ("PBS memory", ""),
            "--pbs-scratch": ("PBS scratch storage", ""),
            "--pbs-walltime": ("PBS walltime", ""),
            "--pbs-image": ("Shared Singularity image", ""),
            "--pbs-blocksci-image": ("BlockSci Singularity image", ""),
            "--pbs-coinjoin-analysis-image": ("coinjoin-analysis Singularity image", ""),
            "--pbs-mappings-enumerator-image": ("Mappings enumerator image", ""),
            "--pbs-sake-image": ("Sake image", ""),
            "--mapping-mining-fee-rate": ("Mapping mining fee rate", "1"),
            "--mapping-coordination-fee-rate": ("Mapping coordination fee rate", "0.003"),
            "--mapping-max-decomposition-fee": ("Maximum decomposition fee", "6000"),
            "--mapping-mode": ("Mapping mode (numeric or all)", "numeric"),
            "--mapping-timeout": ("Initial mapping timeout", "60"),
            "--mapping-retry-timeout": ("Mapping retry timeout", "600"),
            "--sake-seed": ("Sake random seed", "20260704"),
        }
        for flag, (message, default) in prompts.items():
            if flag in advanced:
                add_value(result, flag, message, default)

    def choose_analysis_advanced(result: Command, engine: str, *, blocksci_script: bool) -> None:
        choices = [
            advanced_choice(result, "CoinJoin heuristic type", "--coinjoin-type"),
            advanced_choice(result, "Minimum input count", "--min-input-count"),
            advanced_choice(result, "BlockSci test thresholds", "--test-values"),
        ]
        if engine == "joinmarket":
            choices.extend(
                [
                    advanced_choice(result, "JoinMarket detector mode", "--joinmarket-detector"),
                    advanced_choice(result, "JoinMarket minimum base fee", "--joinmarket-min-base-fee"),
                    advanced_choice(result, "JoinMarket percentage fee", "--joinmarket-percentage-fee"),
                    advanced_choice(result, "JoinMarket maximum search depth", "--joinmarket-max-depth"),
                ]
            )
        if blocksci_script:
            choices.append(advanced_choice(result, "Custom BlockSci Python script", "--blocksci-script"))
        selected = checkbox("Advanced analysis options", choices)
        if "coinjoin-type" in selected:
            value = select(
                "CoinJoin heuristic type",
                ["joinmarket", "wasabi2", "wasabi1", "whirlpool", Choice("Enter another value", "__custom__")],
            )
            if value == "__custom__":
                value = text("CoinJoin heuristic type", validate=_nonempty)
            result.options.append(("--coinjoin-type", value))
        if "min-input-count" in selected:
            add_value(result, "--min-input-count", "Minimum input count")
        add_flag(result, "--test-values", "test-values" in selected)
        if "joinmarket-detector" in selected:
            result.options.append(
                (
                    "--joinmarket-detector",
                    select(
                        "JoinMarket detector",
                        metadata_choices(base_action(result), "--joinmarket-detector"),
                        metadata_default(base_action(result), "--joinmarket-detector"),
                    ),
                )
            )
        detector_values = {
            "joinmarket-min-base-fee": ("--joinmarket-min-base-fee", "Minimum base fee", ""),
            "joinmarket-percentage-fee": ("--joinmarket-percentage-fee", "Percentage fee", ""),
            "joinmarket-max-depth": ("--joinmarket-max-depth", "Maximum subset-search depth", ""),
            "blocksci-script": ("--blocksci-script", "BlockSci Python script path", ""),
        }
        for key, (flag, message, default) in detector_values.items():
            if key in selected:
                add_value(result, flag, message, default)

    def edit_existing_command() -> Command:
        while True:
            pasted = questionary.text(
                "Paste the existing runIt.sh command (Esc+Enter to submit)",
                multiline=True,
                style=style,
            ).ask()
            if pasted is None:
                raise KeyboardInterrupt
            try:
                result = parse_command(pasted)
                break
            except ValueError as error:
                questionary.print(f"Invalid command: {error}", style="bold fg:red")

        while True:
            rendered = render_command(result)
            questionary.print(f"\nCurrent command:\n{rendered}\n", style="fg:#00d7af")
            operation = select(
                "Edit command",
                [
                    Choice("Finish and validate", "finish"),
                    Choice("Change action", "action"),
                    Choice("Change runtime", "runtime"),
                    Choice("Add or replace option", "set"),
                    Choice("Remove option", "remove"),
                ],
                "finish",
            )
            if operation == "action":
                actions = list(command_metadata())
                current_action = (
                    " ".join(result.action.split()[:2])
                    if result.action.startswith(("runs ", "scenarios ", "external "))
                    else result.action
                )
                action = select(
                    "Action",
                    actions,
                    current_action if current_action in actions else "full-run",
                )
                if action in {"scenarios show", "scenarios validate"}:
                    action += f" {shell_quote(text('Scenario name or path', validate=_nonempty))}"
                result.action = action
            elif operation == "runtime":
                result.runtime = select("Runtime", ["docker", "podman"], result.runtime)
            elif operation == "set":
                flag = text("Option name", "--", _nonempty)
                if not flag.startswith("--"):
                    flag = f"--{flag}"
                flag = "--blocksci-script" if flag == "--blocksciScript" else flag
                known_options = all_option_metadata(flag)
                option_takes_value = takes_value(flag)
                if not known_options:
                    option_takes_value = confirm("Does this option take a value?")
                action_option = option_metadata(base_action(result), flag)
                if action_option is not None:
                    help_text = contextual_help(base_action(result), flag)
                    if help_text:
                        questionary.print(f"{flag}: {help_text}", style="fg:cyan")
                if option_takes_value and action_option is not None and action_option.choices:
                    value = select(
                        "Option value",
                        list(action_option.choices),
                        action_option.default_text(),
                    )
                elif option_takes_value:
                    value = text(
                        "Option value",
                        (action_option.default_text() if action_option else "") or "",
                        _nonempty,
                        flag,
                    )
                else:
                    value = None
                if flag == "--false-cjtxs" and has_option(result, flag) and confirm("Append another value?", True):
                    result.options.append((flag, value))
                else:
                    result.options = [(name, old) for name, old in result.options if name != flag]
                    result.options.append((flag, value))
            elif operation == "remove":
                if not result.options:
                    questionary.print("The command has no options.", style="fg:yellow")
                    continue
                selected = select(
                    "Option to remove",
                    [
                        Choice(
                            f"{index + 1}. {flag}{'' if value is None else f' {value}'}",
                            str(index),
                        )
                        for index, (flag, value) in enumerate(result.options)
                    ],
                )
                result.options.pop(int(selected))
            else:
                validation = validate_command(result)
                if validation.errors:
                    questionary.print(
                        "Cannot finish:\n- " + "\n- ".join(validation.errors),
                        style="bold fg:red",
                    )
                    continue
                return result

    mode = select(
        "Starting point",
        [
            Choice("Build a new command", "new"),
            Choice("Paste and edit an existing command", "edit"),
        ],
        "new",
    )
    if mode == "edit":
        return edit_existing_command()

    group = select(
        "Command group",
        [
            Choice("Pipeline", "pipeline"),
            Choice("Runs catalog", "runs"),
            Choice("Scenarios", "scenarios"),
            Choice("External blockchain analysis", "external"),
        ],
        "pipeline",
    )
    runtime = select("Local container runtime", ["docker", "podman"], "docker")

    if group == "runs":
        subcommand = select("Runs command", ["list", "inspect", "validate"], "list")
        result = Command(action=f"runs {subcommand}", runtime=runtime)
        if subcommand in {"inspect", "validate"}:
            add_value(result, "--run-dir", "Run ID")
        if subcommand == "validate" and confirm("Override the BlockSci image?"):
            add_value(result, "--blocksci-image", "BlockSci image")
        return result

    if group == "scenarios":
        subcommand = select("Scenarios command", ["list", "show", "validate"], "list")
        result = Command(action=f"scenarios {subcommand}", runtime=runtime)
        if subcommand == "list":
            if confirm("Filter by engine?"):
                result.options.append(
                    (
                        "--engine",
                        select("Engine", metadata_choices("scenarios list", "--engine")),
                    )
                )
        else:
            scenario = text("Scenario name or path", validate=_nonempty)
            result.action += f" {shell_quote(scenario)}"
            result.options.append(
                (
                    "--engine",
                    select("Engine", metadata_choices(f"scenarios {subcommand}", "--engine")),
                )
            )
        return result

    if group == "external":
        result = Command(action="external analyze", runtime=runtime)
        add_value(result, "--run-id", "External run ID")
        resume = confirm("Resume an existing external run?")
        add_flag(result, "--resume", resume)
        if not resume:
            add_value(result, "--bitcoin-datadir", "Bitcoin Core datadir")
            add_value(result, "--baseline", "coinjoin-analysis baseline JSON")
            while confirm("Add a confirmed false-positive JSON fragment?"):
                add_value(result, "--false-cjtxs", "False-positive JSON file")
        if confirm("Configure advanced external-analysis options?"):
            selected = checkbox(
                "Advanced external options",
                [
                    advanced_choice(result, "Bitcoin network", "--network"),
                    advanced_choice(result, "CoinJoin heuristic type", "--coinjoin-type"),
                    advanced_choice(result, "BlockSci image", "--blocksci-image"),
                    advanced_choice(result, "Minimum free disk space", "--min-free-gb"),
                ],
            )
            if "network" in selected:
                result.options.append(("--network", "bitcoin"))
            if "coinjoin-type" in selected:
                result.options.append(
                    (
                        "--coinjoin-type",
                        select(
                            "CoinJoin type",
                            metadata_choices("external analyze", "--coinjoin-type"),
                            metadata_default("external analyze", "--coinjoin-type"),
                        ),
                    )
                )
            if "blocksci-image" in selected:
                add_value(result, "--blocksci-image", "BlockSci image")
            if "min-free-gb" in selected:
                add_value(result, "--min-free-gb", "Minimum free GiB", "20")
        add_flag(result, "--dry-run", confirm("Validate without launching?"))
        return result

    action_labels = {
        "full-run": "Full run (clean, emulate, analyze)",
        "recreate": "Emulation only",
        "analyze": "Analyze an existing run",
        "export": "Export an existing run",
        "coinjoin-analysis": "coinjoin-analysis only",
        "mappings": "Wasabi mapping enumerator + Sake",
        "initialize": "Initialize/pull required images",
        "clean": "Clean runtime resources",
    }
    pipeline_actions = [action for action in command_metadata() if " " not in action]
    action = select(
        "Pipeline action",
        [Choice(action_labels.get(item, item), item) for item in pipeline_actions],
        "full-run",
    )
    result = Command(action=action, runtime=runtime)

    if action == "clean":
        result.options.append(("--dry-run" if confirm("Preview cleanup only?", True) else "--yes", None))
        return result

    if action == "initialize":
        add_flag(result, "--dry-run", confirm("Preview only?"))
        return result

    if action == "coinjoin-analysis":
        target = select("Runs to analyze", [Choice("One run", "one"), Choice("All runs", "all")])
        if target == "all":
            result.options.append(("--all-runs", None))
        else:
            add_value(result, "--run-dir", "Existing run ID or path")
        result.options.append(
            (
                "--analysis-action",
                select(
                    "Analysis action",
                    metadata_choices("coinjoin-analysis", "--analysis-action"),
                    metadata_default("coinjoin-analysis", "--analysis-action"),
                ),
            )
        )
        choose_pbs(result, allow_analysis=True, allow_blocksci=False)
        add_flag(result, "--dry-run", confirm("Validate without launching?"))
        return result

    engine = select(
        "CoinJoin engine",
        metadata_choices(action, "--engine"),
        "wasabi" if action == "mappings" else "joinmarket",
    )
    result.options.append(("--engine", engine))

    if action == "mappings":
        add_value(result, "--run-dir", "Existing Wasabi run ID or path")
        choose_pbs(result, allow_analysis=False, allow_blocksci=False, allow_mappings=True)
        add_flag(result, "--dry-run", confirm("Validate without launching?"))
        return result

    if action in {"analyze", "export"}:
        add_value(result, "--run-dir", "Existing run ID or path")
        choose_scenario(result)
    else:
        choose_scenario(result)

        driver = select(
            "Emulation driver",
            metadata_choices(action, "--driver"),
            metadata_default(action, "--driver", "docker"),
        )
        if driver == "kubernetes":
            result.options.extend(
                [
                    ("--driver", "kubernetes"),
                    (
                        "--namespace",
                        text(
                            "Kubernetes namespace",
                            metadata_default(action, "--namespace", "coinjoin"),
                            _nonempty,
                        ),
                    ),
                    (
                        "--kubeconfig",
                        text(
                            "Kubeconfig",
                            "${HOME}/.kube/config",
                            _nonempty,
                            "--kubeconfig",
                        ),
                    ),
                ]
            )
            kubernetes_advanced = checkbox(
                "Advanced Kubernetes options",
                [
                    advanced_choice(result, "Reuse existing namespace", "--reuse-namespace", "reuse"),
                    advanced_choice(result, "Pod image registry prefix", "--image-prefix", "image-prefix"),
                    advanced_choice(
                        result,
                        "Direct shared Bitcoin datadir",
                        "--kubernetes-btc-datadir",
                        "kubernetes-btc-datadir",
                    ),
                    advanced_choice(
                        result,
                        "Copy datadir through Kubernetes API",
                        "--copy-to-host",
                        "copy-to-host",
                    ),
                    advanced_choice(
                        result,
                        "Build infrastructure images locally",
                        "--coinjoin-infrastructure-local-build",
                        "local-build",
                    ),
                ],
            )
            add_flag(result, "--reuse-namespace", "reuse" in kubernetes_advanced)
            add_flag(result, "--copy-to-host", "copy-to-host" in kubernetes_advanced)
            add_flag(result, "--coinjoin-infrastructure-local-build", "local-build" in kubernetes_advanced)
            if "image-prefix" in kubernetes_advanced:
                add_value(result, "--image-prefix", "Kubernetes pod image prefix")
            if "kubernetes-btc-datadir" in kubernetes_advanced:
                add_value(result, "--kubernetes-btc-datadir", "Shared Kubernetes Bitcoin datadir")
        elif confirm("Build emulator infrastructure images locally?"):
            result.options.append(("--coinjoin-infrastructure-local-build", None))

        if confirm("Override the run-directory timezone?"):
            add_value(result, "--run-timezone", "IANA timezone")

    if action in {"full-run", "analyze", "export"} and confirm("Configure advanced analysis options?"):
        choose_analysis_advanced(result, engine, blocksci_script=action != "export")

    if action == "full-run":
        choose_pbs(result, allow_analysis=True, allow_blocksci=True, allow_mappings=engine == "wasabi")
    elif action == "analyze":
        choose_pbs(result, allow_analysis=False, allow_blocksci=True)
    if action == "full-run":
        add_flag(result, "--parallel", confirm("Run both analysis stages concurrently?"))

    add_flag(result, "--dry-run", confirm("Add --dry-run to validate without launching?"))
    return result


def main() -> None:
    import questionary
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    console.print(
        Panel(
            "Answer a few questions; no pipeline command will be executed.",
            title="coinjoin-pipeline command builder",
            border_style="cyan",
        )
    )
    try:
        command = collect_command()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Cancelled.[/dim]")
        raise SystemExit(130)
    if base_action(command) in {
        "full-run", "recreate", "clean", "analyze", "export",
        "coinjoin-analysis", "mappings", "initialize", "external analyze",
    }:
        command.version = questionary.text(
            "Coordinated container image version",
            default="latest",
            validate=lambda value: bool(value.strip()) or "Enter an image tag",
        ).ask()
    validation = validate_command(command)
    if validation.errors:
        console.print(
            Panel(
                "\n".join(f"• {error}" for error in validation.errors),
                title="Configuration errors",
                border_style="red",
            )
        )
        raise SystemExit(2)
    rendered = render_command(command)
    console.print("\n[bold cyan]Command explanation[/bold cyan]")
    console.print(Panel("\n".join(explain_command(command)), border_style="cyan", padding=(1, 2)))
    if validation.warnings:
        console.print(
            Panel(
                "\n".join(f"• {warning}" for warning in validation.warnings),
                title="Warnings",
                border_style="yellow",
            )
        )
    console.print("\n[bold cyan]Generated command[/bold cyan]")
    console.print(Panel(rendered, border_style="green", padding=(1, 2)))
    action = base_action(command)
    if "--dry-run" in parser_flags(action) and sys.stdin.isatty():
        should_run = questionary.confirm(
            "Run the generated command's --dry-run preflight now?",
            default=False,
        ).ask()
        if should_run:
            preview = preflight_command(command)
            console.print(
                Panel(
                    render_command(preview),
                    title="Executing preflight",
                    border_style="cyan",
                )
            )
            return_code = run_preflight(command)
            style_name = "green" if return_code == 0 else "red"
            console.print(
                Panel(
                    f"Preflight exit code: {return_code}",
                    border_style=style_name,
                )
            )
            if return_code:
                raise SystemExit(return_code)


if __name__ == "__main__":
    main()
