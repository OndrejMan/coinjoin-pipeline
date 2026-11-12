"""EXPERIMENTAL: Local container commands and their filesystem inputs.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.scenarios import container_scenario_path, scenario_source_path
from coinjoin_pipeline.images import default_image
from coinjoin_pipeline.paths import default_runs_root, scenarios_dir

from . import settings

DEFAULT_CONFIGURATION = PipelineConfiguration()


def run_command(command: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> None:
    """Stream a child command's merged stdout/stderr through the active stage tee."""
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        env=env,
    ) as process:
        assert process.stdout is not None
        with process.stdout:
            for line in process.stdout:
                print(line, end="", flush=True)
        return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)


def compose_project_name(host_root_dir: Path) -> str:
    """Return a stable Compose project name isolated to one pipeline checkout."""
    checkout_hash = hashlib.sha256(str(host_root_dir).encode("utf-8")).hexdigest()[:12]
    return f"{settings.COMPOSE_PROJECT}-{checkout_hash}"


def compose_env(
    config: PipelineConfiguration = DEFAULT_CONFIGURATION,
    *,
    include_scenario: bool = True,
) -> dict[str, str]:
    """Build the Compose environment for one configuration and optional run."""
    env = os.environ.copy()
    host_root_dir = settings.ROOT_DIR
    runs_root = default_runs_root()
    emulation_logs_dir = runs_root
    env.setdefault(settings.COMPOSE_PROJECT_ENV, compose_project_name(host_root_dir))
    env.setdefault("EMULATION_LOGS_DIR", str(emulation_logs_dir))
    scenario_root = scenarios_dir()
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    env["SCENARIOS_DIR"] = str(scenario_root)
    env["EMULATION_LOGS_DIR"] = str(emulation_logs_dir)
    env["COINJOIN_ENGINE"] = config.engine
    env["RUN_TIMEZONE"] = config.run_timezone
    env.setdefault("COINJOIN_EMULATOR_IMAGE", default_image("emulator"))
    scenario = config.scenario if include_scenario else None
    if scenario:
        source = scenario_source_path(scenario, scenario_root, config.engine)
        if not source.is_relative_to(scenario_root):
            scenario_root = source.parent
            env["SCENARIOS_DIR"] = str(scenario_root)
        scenario = str(source)
    env["SCENARIO_CONTAINER_PATH"] = container_scenario_path(scenario, scenario_root, config.engine)
    return env


def run_script(
    script: Path,
    *args: str,
    config: PipelineConfiguration = DEFAULT_CONFIGURATION,
    include_scenario: bool = True,
) -> None:
    if not script.exists():
        raise RuntimeError(f"script not found: {script}")
    env = compose_env(config, include_scenario=include_scenario)
    if script == settings.EMULATE_SCRIPT:
        relative_scenario = env["SCENARIO_CONTAINER_PATH"].removeprefix(settings.CONTAINER_SCENARIOS_DIR + "/")
        args = ("--scenario", "/app/scenarios/" + relative_scenario)
    try:
        run_command(["bash", str(script), *args], cwd=settings.ROOT_DIR, env=env)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
