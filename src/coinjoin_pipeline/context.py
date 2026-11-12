"""Resolve one experiment and own its local execution lifetime."""

from __future__ import annotations

import os
import platform
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from .configuration import (
    ConfigurationError,
    PipelineConfiguration,
)
from .images import Images, resolve_images
from .paths import EXPORTERS_ROOT, default_runs_root, scenarios_dir
from .runs import run_id_for


def runs_root(config: PipelineConfiguration) -> Path:
    """The configured runs root, else the default one, as an absolute path."""
    return Path(config.runs_root).expanduser().resolve() if config.runs_root else default_runs_root()


def resolve_configuration(config: PipelineConfiguration) -> PipelineConfiguration:
    try:
        ZoneInfo(config.run_timezone)
    except (KeyError, ValueError) as error:
        raise ConfigurationError(f"invalid run timezone: {config.run_timezone}") from error
    config = replace(config, runs_root=str(runs_root(config)))
    provided = set(config.provided)
    if config.engine == "joinmarket" and "coinjoin_type" not in provided:
        config = replace(config, coinjoin_type="joinmarket")
    if config.action in {"full-run", "emulate"} and not config.run_dir:
        config = replace(config, run_id=run_id_for(config))
        provided.add("run_id")
    config = replace(config, _provided=frozenset(provided))
    if config.scenario and Path(config.scenario).expanduser().is_file():
        config = replace(config, scenario=str(Path(config.scenario).expanduser().resolve()))
    config.validate()
    return config


def resolve_run_images(config: PipelineConfiguration) -> tuple[PipelineConfiguration, Images]:
    """Pin every image reference of the component images."""
    images = resolve_images(
        config.images.version,
        {item.name: getattr(config.images, item.name) for item in fields(Images)},
    )
    config = replace(
        config,
        images=replace(
            config.images,
            emulator=images.emulator,
            coinjoin_analysis=images.coinjoin_analysis,
            blocksci=images.blocksci,
        ),
    )
    return config, images


def selected_run_dir(config: PipelineConfiguration) -> Path | None:
    """--run-dir relative to the runs root, else the directory of the run ID."""
    if config.run_dir:
        selected = Path(config.run_dir).expanduser()
        return (selected if selected.is_absolute() else config.runs_path / selected).resolve()
    return config.runs_path / config.run_id if config.run_id else None


def host_environment(config: PipelineConfiguration, images: Images) -> dict[str, str]:
    """Variables the launched stages read, set only while the run executes."""
    root = config.runs_path
    environment = {
        "CONTAINER_RUNTIME": config.runtime,
        "EXPORTERS_DIR": str(EXPORTERS_ROOT),
        "SCENARIOS_DIR": str(scenarios_dir()),
        "EMULATION_LOGS_DIR": str(root),
        "COINJOIN_EMULATOR_IMAGE": images.emulator,
        "COINJOIN_ANALYSIS_IMAGE": images.coinjoin_analysis,
        "BLOCKSCI_IMAGE": images.blocksci,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if config.run_id:
        environment["PIPELINE_RUN_ID"] = config.run_id
    if config.engine == "joinmarket" and platform.machine() in {"arm64", "aarch64"}:
        environment["COINJOIN_EMULATOR_DOCKER_PLATFORM"] = (
            os.environ.get("COINJOIN_EMULATOR_DOCKER_PLATFORM") or "linux/amd64"
        )
    return environment


@dataclass(frozen=True)
class RunContext:
    config: PipelineConfiguration
    images: Images
    environment: dict[str, str]
    run_dir: Path | None

    @classmethod
    def prepare(cls, config: PipelineConfiguration) -> RunContext:
        config, images = resolve_run_images(resolve_configuration(config))
        return cls(config, images, host_environment(config, images), selected_run_dir(config))

    @contextmanager
    def activate(self):
        previous = dict(os.environ)
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        os.environ.update(self.environment)
        try:
            yield
        finally:
            sys.dont_write_bytecode = previous_bytecode
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            os.environ.clear()
            os.environ.update(previous)
