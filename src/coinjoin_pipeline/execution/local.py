"""EXPERIMENTAL: local Docker actions.

These paths serve local development and reproduction.
"""

from __future__ import annotations

from pathlib import Path

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.pipeline_logging import captured_pipeline_stage, exit_with_error
from coinjoin_pipeline.execution.run_context import detect_active_run, run_dir_under_root

from . import containers, settings


def _emulate(args: PipelineConfiguration, logs_root: Path) -> Path:
    """Run the emulator on Docker and file its log under the run it created."""
    with captured_pipeline_stage(logs_root, "Docker emulation") as log:
        containers.run_script(settings.EMULATE_SCRIPT, config=args)
    active_run = detect_active_run(logs_root)
    if active_run is None:
        log.relocate(logs_root / "_failed")
        exit_with_error(RuntimeError("Emulator did not produce the expected run directory."))
    print(f"Active run: {active_run.name}")
    log.relocate_to_run(active_run)
    return active_run


def _selected_run_dir(args: PipelineConfiguration, logs_root: Path) -> Path:
    return run_dir_under_root(required(args.run_dir, "run_dir"), logs_root)


def run_configuration(args: PipelineConfiguration, logs_root: Path) -> None:
    action = args.action
    if action == "emulate":
        _emulate(args, logs_root)
    elif action == "analyze":
        run_dir = _selected_run_dir(args, logs_root)
        with captured_pipeline_stage(logs_root, "BlockSci analysis", run_dir):
            containers.run_script(settings.ANALYSIS_SCRIPT, config=args, active_run_id=run_dir.name)
    elif action == "coinjoin-analysis":
        containers.run_coinjoin_analysis(args.run_dir)
