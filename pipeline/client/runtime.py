"""Container runtime and Compose command selection."""

from __future__ import annotations

import os
import shlex
from typing import Mapping

from client.cli_options import CONTAINER_RUNTIMES

DEFAULT_CONTAINER_RUNTIME = "docker"
CONTAINER_RUNTIME_ENV = "CONTAINER_RUNTIME"
CONTAINER_COMPOSE_COMMAND_ENV = "CONTAINER_COMPOSE_COMMAND"
VALID_CONTAINER_RUNTIMES = CONTAINER_RUNTIMES


def container_runtime(env: Mapping[str, str] | None = None) -> str:
    runtime_env = os.environ if env is None else env
    runtime = runtime_env.get(CONTAINER_RUNTIME_ENV, DEFAULT_CONTAINER_RUNTIME).strip()
    if runtime not in VALID_CONTAINER_RUNTIMES:
        raise ValueError(
            f"Unsupported container runtime '{runtime}'. "
            f"Expected one of: {', '.join(VALID_CONTAINER_RUNTIMES)}."
        )
    return runtime


def container_command(env: Mapping[str, str] | None = None) -> list[str]:
    return [container_runtime(env)]


def compose_command(env: Mapping[str, str] | None = None) -> list[str]:
    runtime_env = os.environ if env is None else env
    override = runtime_env.get(CONTAINER_COMPOSE_COMMAND_ENV, "").strip()
    if override:
        return shlex.split(override)
    return [container_runtime(runtime_env), "compose"]
