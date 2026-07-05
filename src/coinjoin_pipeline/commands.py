"""Dependency-free host command parsing, validation, and rendering."""

from __future__ import annotations

from dataclasses import dataclass
import json
from importlib.resources import files
from pathlib import Path
import shlex
from typing import Any

from .images import Images


MUTATING_ACTIONS = {
    "full-run", "recreate", "analyze", "export", "coinjoin-analysis",
    "coinjoin", "mappings", "initialize", "external analyze", "clean",
    "pbs-from-s3",
}
RESEARCH_PREFIXES = {"runs", "scenarios", "external"}


def metadata() -> dict[str, Any]:
    path = files("coinjoin_pipeline").joinpath("metadata/command_metadata.json")
    return json.loads(path.read_text(encoding="utf-8"))


def known_actions() -> set[str]:
    return set(metadata()["commands"])


def action_from(argv: list[str]) -> str:
    words = [item for item in argv if not item.startswith("-")]
    if len(words) >= 2 and f"{words[0]} {words[1]}" in known_actions():
        return f"{words[0]} {words[1]}"
    if words and words[0] == "coinjoin":
        return "coinjoin-analysis"
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
    if has_option(argv, "--analysisPbs") and action not in {"full-run", "coinjoin-analysis", "coinjoin", "pbs-from-s3"}:
        errors.append("--analysisPbs is supported only by full-run and coinjoin-analysis")
    if has_option(argv, "--blocksciPbs") and action not in {"full-run", "analyze", "pbs-from-s3"}:
        errors.append("--blocksciPbs is supported only by full-run and analyze")
    if has_option(argv, "--mappingsPbs") and action not in {"full-run", "mappings"}:
        errors.append("--mappingsPbs is supported only by full-run and mappings")
    backend = option_value(argv, "--artifact-backend") or "shared-storage"
    if action == "pbs-from-s3":
        for flag in ("--run-id", "--artifact-uri", "--s3-endpoint-url", "--s3-credentials-file", "--s3-profile", "--engine"):
            if not has_option(argv, flag):
                errors.append(f"pbs-from-s3 requires {flag}")
        if not has_option(argv, "--analysisPbs") and not has_option(argv, "--blocksciPbs"):
            errors.append("pbs-from-s3 requires --analysisPbs or --blocksciPbs")
    if backend == "s3" and action == "full-run":
        errors.append(
            "S3-compatible full-run orchestration is not implemented yet. "
            "Use independent recreate --artifact-backend s3 and pbs-from-s3 workflows."
        )
    if backend == "s3" and action == "recreate":
        for flag in ("--run-id", "--artifact-uri", "--s3-endpoint-url", "--s3-secret-name"):
            if not has_option(argv, flag):
                errors.append(f"Kubernetes S3-compatible mode requires {flag}")
        if option_value(argv, "--driver") != "kubernetes":
            errors.append("--artifact-backend s3 requires --driver kubernetes")
        for flag in ("--kubernetes-btc-datadir", "--pbs-bitcoin-datadir", "--copy-to-host"):
            if has_option(argv, flag):
                errors.append(f"Kubernetes S3-compatible mode does not support {flag}")
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
