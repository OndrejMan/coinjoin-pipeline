"""EXPERIMENTAL: Container runtime and Compose command selection.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

import os
import shlex
from typing import Mapping, get_args

from coinjoin_pipeline.configuration import ContainerRuntime

DEFAULT_CONTAINER_RUNTIME = "docker"
CONTAINER_RUNTIME_ENV = "CONTAINER_RUNTIME"
CONTAINER_COMPOSE_COMMAND_ENV = "CONTAINER_COMPOSE_COMMAND"
VALID_CONTAINER_RUNTIMES = get_args(ContainerRuntime)


def container_runtime(env: Mapping[str, str] | None = None) -> str:
    runtime_env = os.environ if env is None else env
    runtime = runtime_env.get(CONTAINER_RUNTIME_ENV, DEFAULT_CONTAINER_RUNTIME).strip()
    if runtime not in VALID_CONTAINER_RUNTIMES:
        raise ValueError(
            f"Unsupported container runtime '{runtime}'. Expected one of: {', '.join(VALID_CONTAINER_RUNTIMES)}."
        )
    return runtime


def compose_command(env: Mapping[str, str] | None = None) -> list[str]:
    runtime_env = os.environ if env is None else env
    override = runtime_env.get(CONTAINER_COMPOSE_COMMAND_ENV, "").strip()
    if override:
        return shlex.split(override)
    return [container_runtime(runtime_env), "compose"]
