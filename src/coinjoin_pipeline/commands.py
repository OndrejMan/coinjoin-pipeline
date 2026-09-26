"""Dependency-free host command parsing, validation, and rendering."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import json
from importlib.resources import files
import os
from pathlib import Path
import platform
import shlex
import sys
from typing import Any

from .images import Images
from .option_rules import (  # noqa: F401 - re-exported for existing importers
    ACTION_ALIASES,
    PBS_STAGE_ACTIONS,
    RUN_ID_RE,
    cross_option_errors,
)


MUTATING_ACTIONS = {
    "full-run", "emulate", "analyze", "export", "coinjoin-analysis",
    "coinjoin", "mappings", "initialize", "external analyze", "clean",
    "pbs-from-s3",
}
RESEARCH_PREFIXES = {"runs", "scenarios", "external"}
# Research actions that stay in-process and may therefore skip the container
# runtime and image preflight. `runs validate` is deliberately absent: it starts
# the BlockSci image to reopen the parsed chain (research.py:validate_existing_run),
# so exempting it turns a clear preflight error into a late runtime failure.
DOCKERLESS_RESEARCH_ACTIONS = frozenset(
    {"runs list", "runs inspect", "scenarios list", "scenarios show", "scenarios validate"}
)


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


def option_value(argv: list[str], flag: str, *aliases: str) -> str | None:
    """Read the last occurrence, matching argparse's store action."""
    flags = (flag, *aliases)
    for index in range(len(argv) - 1, -1, -1):
        item = argv[index]
        if item in flags:
            return argv[index + 1] if index + 1 < len(argv) else None
        if item.split("=", 1)[0] in flags and "=" in item:
            return item.split("=", 1)[1]
    return None


@dataclass(frozen=True)
class ArgvOptions:
    """:class:`option_rules.OptionView` over a raw argv list."""

    argv: list[str]

    def has(self, flag: str) -> bool:
        return has_option(self.argv, flag)

    def value(self, flag: str) -> str | None:
        return option_value(self.argv, flag)


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
        value = option_value(argv, *option_aliases)
        if option["takes_value"] and (value is None or value.startswith("--")):
            errors.append(f"{present_alias} requires a value")
            continue
        if option["choices"] and value not in option["choices"]:
            errors.append(f"{present_alias} must be one of: {', '.join(option['choices'])}")
    errors.extend(cross_option_errors(ArgvOptions(argv), action))
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


def prepend_path(entry: Path, existing: str | None) -> str:
    """Put ``entry`` first on a ``PATH``-style variable without dropping the rest."""
    return f"{entry}{os.pathsep}{existing}" if existing else str(entry)


# Variables the wrapper and the pipeline shell scripts read straight from the
# environment. The launcher container only ever saw what it was handed with -e;
# the bare wrapper inherits the whole shell, so these are folded into the
# rendered command instead — otherwise a run could be steered by a variable that
# neither the printed command nor the manifest mentions.
INHERITED_ENVIRONMENT_PREFIXES = (
    "BLOCKSCI_", "COINJOIN_", "KUBERNETES_", "MAPPINGS_", "SAKE_", "PBS_",
)
# Never render or store a value that can carry a credential.
SECRET_ENVIRONMENT_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "ACCESS_KEY", "CREDENTIAL")


def inherited_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Pipeline-relevant variables the caller exported, minus anything secret."""
    items = os.environ if source is None else source
    return {
        name: value
        for name, value in items.items()
        if name.startswith(INHERITED_ENVIRONMENT_PREFIXES)
        and not any(marker in name for marker in SECRET_ENVIRONMENT_MARKERS)
    }


def runtime_environment(
    runtime_root: Path, runtime: str, images: Images,
    runs_root: Path, reproduction_command: str, engine: str | None = None,
) -> dict[str, str]:
    """Environment contract between the host CLI and the bare wrapper.

    Everything here was previously computed by the in-image launcher. Values the
    user exported are inherited by the subprocess anyway; the pipeline-relevant
    ones are restated here so the rendered command reproduces the run, while
    computed values still win over whatever the shell happened to hold.
    """
    checkout_root = runtime_root.parent
    environment = {
        "CONTAINER_RUNTIME": runtime,
        "PYTHONPATH": prepend_path(runtime_root, os.environ.get("PYTHONPATH")),
        # The wrapper now runs straight out of the checkout; without this it
        # would litter pipeline/ with __pycache__ that later ships to S3.
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOST_CLIENT_DIR": str(runtime_root / "client"),
        "EXPORTERS_DIR": str(runtime_root / "exporters"),
        "SCENARIOS_DIR": str(checkout_root / "scenarios"),
        "NOTEBOOKS_DIR": str(runs_root / ".notebooks"),
        "EMULATION_LOGS_DIR": str(runs_root),
        # compose.yaml defaults this to "true"; the launcher effectively sent 0.
        "BLOCKSCI_LAUNCH_JUPYTER": os.environ.get("BLOCKSCI_LAUNCH_JUPYTER") or "0",
        "COINJOIN_EMULATOR_IMAGE": images.emulator,
        "COINJOIN_ANALYSIS_IMAGE": images.coinjoin_analysis,
        "BLOCKSCI_IMAGE": images.blocksci,
        "MAPPINGS_ENUMERATOR_IMAGE": images.mappings,
        "SAKE_IMAGE": images.sake,
        "REPRODUCTION_COMMAND": reproduction_command,
    }
    if engine == "joinmarket" and not os.environ.get("COINJOIN_EMULATOR_DOCKER_PLATFORM"):
        if platform.machine() in {"arm64", "aarch64"}:
            environment["COINJOIN_EMULATOR_DOCKER_PLATFORM"] = "linux/amd64"
    return {**inherited_environment(), **environment}


def runtime_command(
    runtime_root: Path, runtime: str, passthrough: list[str], images: Images,
    runs_root: Path, reproduction_command: str,
) -> RuntimeCommand:
    """Build the bare wrapper invocation that replaced the launcher container."""
    environment = runtime_environment(
        runtime_root, runtime, images, runs_root, reproduction_command,
        engine=option_value(passthrough, "--engine"),
    )
    # wrapper.py rejects --copy-to-host without an explicit host directory; the
    # launcher supplied this default before handing over.
    if has_option(passthrough, "--copy-to-host") and not os.environ.get(
        "KUBERNETES_COPY_TO_HOST_DIR"
    ):
        environment["KUBERNETES_COPY_TO_HOST_DIR"] = str(runs_root / ".kubernetes-btc-data")
    arguments = (str(runtime_root / "client" / "wrapper.py"), *passthrough)
    return RuntimeCommand(sys.executable, arguments, environment)


def research_command(
    runtime_root: Path, runtime: str, passthrough: list[str], images: Images,
    runs_root: Path, reproduction_command: str,
) -> RuntimeCommand:
    """``runs``/``external``/``scenarios`` run the same way, as a module.

    ``-m client.research`` keeps ``__package__ == "client"`` exactly as the
    launcher's ``python3 -m client.research`` did, so absolute ``from client.X``
    imports resolve without relying on research.py's own sys.path fallback.
    """
    environment = runtime_environment(
        runtime_root, runtime, images, runs_root, reproduction_command,
    )
    # research.py takes --runs-root/--runtime as global options, so they must
    # precede the subcommand exactly as the launcher passed them.
    arguments = (
        "-m", "client.research",
        "--runs-root", str(runs_root), "--runtime", runtime,
        *passthrough,
    )
    return RuntimeCommand(sys.executable, arguments, environment)
