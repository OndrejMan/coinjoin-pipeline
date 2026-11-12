"""EXPERIMENTAL: local Docker actions.

These paths serve local development and reproduction.
"""

from __future__ import annotations

from pathlib import Path

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.run_context import detect_active_run

from . import containers, settings


def _emulate(args: PipelineConfiguration, logs_root: Path) -> Path:
    """Run the emulator on Docker and return the run it created."""
    containers.run_script(settings.EMULATE_SCRIPT, config=args)
    active_run = detect_active_run(logs_root)
    if active_run is None:
        raise RuntimeError("Emulator did not produce the expected run directory.")
    print(f"Active run: {active_run.name}")
    return active_run


def run_configuration(args: PipelineConfiguration, logs_root: Path) -> None:
    action = args.action
    if action == "emulate":
        _emulate(args, logs_root)
