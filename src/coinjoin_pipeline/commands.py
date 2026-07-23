"""Dependency-free host command parsing, validation, and rendering."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from importlib.resources import files
from pathlib import Path
import shlex
from typing import Any

from .images import Images


MUTATING_ACTIONS = {
    "full-run", "emulate", "analyze", "export", "coinjoin-analysis",
    "coinjoin", "mappings", "initialize", "external analyze", "clean",
    "pbs-from-s3",
}
RESEARCH_PREFIXES = {"runs", "scenarios", "external"}
# Alias -> canonical action. Aliases are accepted but never named in error text.
ACTION_ALIASES = {"coinjoin": "coinjoin-analysis"}
# Actions each PBS offload flag may accompany; also the source of the error text.
PBS_STAGE_ACTIONS = {
    "--analysisPbs": ("full-run", "coinjoin-analysis", "coinjoin", "pbs-from-s3"),
    "--blocksciPbs": ("full-run", "analyze", "pbs-from-s3"),
    "--mappingsPbs": ("full-run", "mappings", "pbs-from-s3"),
}


def metadata() -> dict[str, Any]:
    path = files("coinjoin_pipeline").joinpath("metadata/command_metadata.json")
    return json.loads(path.read_text(encoding="utf-8"))


def known_actions() -> set[str]:
    return set(metadata()["commands"])


@lru_cache(maxsize=1)
def value_taking_aliases() -> frozenset[str]:
    """Every option alias that consumes the following argv token."""
    aliases: set[str] = set()
    for command in metadata()["commands"].values():
        for option in command["options"].values():
            if option["takes_value"]:
                aliases.update(option["aliases"])
    return frozenset(aliases)


def action_from(argv: list[str]) -> str:
    # Option *values* must not be mistaken for the action word, so skip the
    # token after any value-taking flag (mirrors the wrapper's normalize_argv).
    value_aliases = value_taking_aliases()
    words: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item in value_aliases:
            skip_next = True
            continue
        if item.startswith("-"):
            continue
        words.append(item)
    if len(words) >= 2 and f"{words[0]} {words[1]}" in known_actions():
        return f"{words[0]} {words[1]}"
    if words and words[0] in ACTION_ALIASES:
        return ACTION_ALIASES[words[0]]
    if words and words[0] in known_actions():
        return words[0]
    return "full-run"


def has_option(argv: list[str], flag: str) -> bool:
    return flag in argv or any(item.startswith(f"{flag}=") for item in argv)


def option_value(argv: list[str], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag:
            return argv[index + 1] if index + 1 < len(argv) else None
        if item.startswith(f"{flag}="):
            return item.split("=", 1)[1]
    return None


def validate_passthrough(argv: list[str], action: str) -> list[str]:
    errors: list[str] = []
    commands = metadata()["commands"]
    if action not in commands:
        return [f"unsupported action: {action}"]
    supported = commands[action]["options"]
    aliases = {alias for option in supported.values() for alias in option["aliases"]}
    for item in argv:
        if item.startswith("--"):
            flag = item.split("=", 1)[0]
            if flag not in aliases:
                errors.append(f"{action} does not support {flag}")
    for option in supported.values():
        option_aliases = option["aliases"]
        present_alias = next((alias for alias in option_aliases if has_option(argv, alias)), None)
        if option["required"] and present_alias is None:
            errors.append(f"{action} requires {option['flag']}")
        if present_alias is None:
            continue
        value = option_value(argv, present_alias)
        if option["takes_value"] and (value is None or value.startswith("--")):
            errors.append(f"{present_alias} requires a value")
            continue
        if option["choices"] and value not in option["choices"]:
            errors.append(f"{present_alias} must be one of: {', '.join(option['choices'])}")
    if action in {"analyze", "export", "coinjoin-analysis", "coinjoin", "mappings"}:
        if not has_option(argv, "--run-dir") and not has_option(argv, "--all-runs"):
            errors.append(f"{action} requires --run-dir (or --all-runs where supported)")
    if action == "clean" and not has_option(argv, "--dry-run") and not has_option(argv, "--yes"):
        errors.append("clean is destructive; pass --yes or --dry-run")
    if option_value(argv, "--driver") != "kubernetes":
        for flag in ("--kubeconfig", "--namespace", "--reuse-namespace", "--copy-to-host"):
            if has_option(argv, flag):
                errors.append(f"{flag} requires --driver kubernetes")
    for flag, permitted in PBS_STAGE_ACTIONS.items():
        if has_option(argv, flag) and action not in permitted:
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
        if any(has_option(argv, flag) for flag in stage_resources) and not has_option(
            argv, enabling_flag
        ):
            errors.append(f"{stage}-specific PBS resources require {enabling_flag}")
    backend = option_value(argv, "--artifact-backend") or "shared-storage"
    blocksci_workflow = option_value(argv, "--blocksci-workflow") or "combined"
    blocksci_task = option_value(argv, "--blocksci-task") or "detect"
    if blocksci_workflow != "combined" and not has_option(argv, "--blocksciPbs"):
        errors.append("reusable BlockSci workflows require --blocksciPbs")
    if blocksci_task == "parse":
        if action != "pbs-from-s3" or blocksci_workflow != "reusable":
            errors.append(
                "--blocksci-task parse requires pbs-from-s3 --blocksci-workflow reusable"
            )
        if has_option(argv, "--analysisPbs") or not has_option(argv, "--blocksciPbs"):
            errors.append(
                "--blocksci-task parse requires --blocksciPbs without --analysisPbs"
            )
    elif blocksci_task == "update":
        if action != "pbs-from-s3" or blocksci_workflow != "cached":
            errors.append(
                "--blocksci-task update requires pbs-from-s3 --blocksci-workflow cached"
            )
        if has_option(argv, "--analysisPbs") or not has_option(argv, "--blocksciPbs"):
            errors.append(
                "--blocksci-task update requires --blocksciPbs without --analysisPbs"
            )
    elif blocksci_task != "detect":
        if action != "pbs-from-s3":
            errors.append("BlockSci script and notebook tasks are submitted with pbs-from-s3")
        if blocksci_workflow == "combined":
            errors.append(
                "BlockSci script and notebook tasks require --blocksci-workflow reusable or cached"
            )
        if has_option(argv, "--analysisPbs") or not has_option(argv, "--blocksciPbs"):
            errors.append(
                "BlockSci script and notebook tasks require --blocksciPbs without --analysisPbs"
            )
    if action == "pbs-from-s3" and blocksci_task == "script" and not (
        has_option(argv, "--blocksci-script") or has_option(argv, "--blocksciScript")
    ):
        errors.append("--blocksci-task script requires --blocksci-script")
    if action == "pbs-from-s3" and blocksci_task != "script" and (
        has_option(argv, "--blocksci-script") or has_option(argv, "--blocksciScript")
    ):
        errors.append("--blocksci-script requires --blocksci-task script")
    if blocksci_task != "notebook" and has_option(argv, "--blocksci-notebooks-dir"):
        errors.append("--blocksci-notebooks-dir requires --blocksci-task notebook")
    if blocksci_task != "notebook" and has_option(argv, "--blocksci-notebook-port"):
        errors.append("--blocksci-notebook-port requires --blocksci-task notebook")
    external_bitcoin = has_option(argv, "--blocksci-external-bitcoin-datadir")
    external_index = has_option(argv, "--blocksci-external-blocksci-dir")
    external_network = has_option(argv, "--blocksci-network")
    external_max_block = has_option(argv, "--blocksci-max-block")
    source_cache_run_id = option_value(argv, "--blocksci-cache-source-run-id")
    if blocksci_task == "update":
        if not source_cache_run_id:
            errors.append("--blocksci-task update requires --blocksci-cache-source-run-id")
        if not external_bitcoin:
            errors.append("--blocksci-task update requires --blocksci-external-bitcoin-datadir")
        if external_index:
            errors.append("--blocksci-task update does not support --blocksci-external-blocksci-dir")
        target_run_id = option_value(argv, "--run-id")
        if source_cache_run_id and target_run_id == source_cache_run_id:
            errors.append("--blocksci-cache-source-run-id must differ from target --run-id")
    elif source_cache_run_id:
        errors.append("--blocksci-cache-source-run-id requires --blocksci-task update")
    if external_bitcoin and external_index:
        errors.append(
            "choose either --blocksci-external-bitcoin-datadir or "
            "--blocksci-external-blocksci-dir, not both"
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
            errors.append(
                "external BlockSci sources require either reusable parse or cached update"
            )
    if external_bitcoin:
        if not external_network or not external_max_block:
            errors.append(
                "--blocksci-external-bitcoin-datadir requires --blocksci-network "
                "and --blocksci-max-block"
            )
    elif external_network or external_max_block:
        errors.append(
            "--blocksci-network and --blocksci-max-block require "
            "--blocksci-external-bitcoin-datadir"
        )
    if action == "pbs-from-s3":
        for flag in ("--run-id", "--artifact-uri", "--s3-endpoint-url", "--s3-credentials-file", "--s3-profile", "--engine"):
            if not has_option(argv, flag):
                errors.append(f"pbs-from-s3 requires {flag}")
        if not any(
            has_option(argv, flag)
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
            has_option(argv, "--blocksciPbs")
            and blocksci_task == "detect"
            and (
                has_option(argv, "--analysisPbs")
                or has_option(argv, "--mappingsPbs")
                or blocksci_workflow != "combined"
            )
        )
        if any(has_option(argv, flag) for flag in report_resource_flags) and not separate_report:
            errors.append(
                "unified-report PBS resource overrides require a separate unified-report job"
            )
    if backend == "s3" and action == "full-run":
        if blocksci_workflow == "cached":
            errors.append(
                "full-run cannot reuse a cache before emulation; use --blocksci-workflow reusable"
            )
        if option_value(argv, "--driver") != "kubernetes":
            errors.append("full-run --artifact-backend s3 requires --driver kubernetes")
        for flag in (
            "--run-id",
            "--artifact-uri",
            "--s3-endpoint-url",
            "--s3-secret-name",
            "--s3-credentials-file",
            "--s3-profile",
        ):
            if not has_option(argv, flag):
                errors.append(f"full-run --artifact-backend s3 requires {flag}")
        if not has_option(argv, "--analysisPbs") or not has_option(argv, "--blocksciPbs"):
            errors.append("full-run --artifact-backend s3 requires both --analysisPbs and --blocksciPbs")
        if not has_option(argv, "--reuse-namespace"):
            errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created"
            )
        if has_option(argv, "--parallel"):
            errors.append("full-run --artifact-backend s3 does not support --parallel")
        if has_option(argv, "--blocksci-script") or has_option(argv, "--blocksciScript"):
            errors.append("full-run --artifact-backend s3 does not support --blocksci-script")
        for flag in ("--kubernetes-btc-datadir", "--pbs-bitcoin-datadir", "--copy-to-host"):
            if has_option(argv, flag):
                errors.append(f"Kubernetes S3-compatible mode does not support {flag}")
    if backend == "s3" and action == "emulate":
        for flag in ("--run-id", "--artifact-uri", "--s3-endpoint-url", "--s3-secret-name"):
            if not has_option(argv, flag):
                errors.append(f"Kubernetes S3-compatible mode requires {flag}")
        if option_value(argv, "--driver") != "kubernetes":
            errors.append("--artifact-backend s3 requires --driver kubernetes")
        if not has_option(argv, "--reuse-namespace"):
            errors.append(
                "Kubernetes S3-compatible mode requires --reuse-namespace because "
                "the credentials Secret must exist before the Job is created"
            )
        for flag in ("--kubernetes-btc-datadir", "--pbs-bitcoin-datadir", "--copy-to-host"):
            if has_option(argv, flag):
                errors.append(f"Kubernetes S3-compatible mode does not support {flag}")
    if action == "full-run" and backend != "s3" and (
        blocksci_workflow != "combined" or blocksci_task != "detect"
    ):
        errors.append(
            "reusable BlockSci workflows are currently supported only with the S3 artifact backend"
        )
    engine = option_value(argv, "--engine")
    if engine is not None and engine not in {"wasabi", "joinmarket"}:
        errors.append("--engine must be wasabi or joinmarket")
    script = option_value(argv, "--blocksci-script") or option_value(argv, "--blocksciScript")
    if script and not Path(script).expanduser().is_file():
        errors.append(f"BlockSci script not found: {script}")
    return list(dict.fromkeys(errors))


@dataclass(frozen=True)
class RuntimeCommand:
    executable: str
    arguments: tuple[str, ...]
    environment: dict[str, str]

    def argv(self) -> list[str]:
        return [self.executable, *self.arguments]

    def rendered(self) -> str:
        env = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(self.environment.items()))
        command = shlex.join(self.argv())
        return f"{env} {command}" if env else command


def launcher_command(
    launcher: Path, runtime: str, passthrough: list[str], images: Images,
    runs_root: Path, reproduction_command: str,
) -> RuntimeCommand:
    arguments = tuple(["container", runtime, *passthrough])
    environment = {
        "CONTAINER_RUNTIME": runtime,
        "EMULATION_LOGS_DIR": str(runs_root),
        "WRAPPER_IMAGE": images.pipeline,
        "COINJOIN_EMULATOR_IMAGE": images.emulator,
        "COINJOIN_ANALYSIS_IMAGE": images.coinjoin_analysis,
        "BLOCKSCI_IMAGE": images.blocksci,
        "MAPPINGS_ENUMERATOR_IMAGE": images.mappings,
        "SAKE_IMAGE": images.sake,
        "REPRODUCTION_COMMAND": reproduction_command,
    }
    return RuntimeCommand(str(launcher), arguments, environment)
