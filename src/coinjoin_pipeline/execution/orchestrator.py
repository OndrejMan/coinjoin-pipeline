"""Dispatch a resolved configuration to the experimental local paths in ``local.py``."""

from __future__ import annotations

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.locks import acquire_lock, command_lock_path
from coinjoin_pipeline.execution.pipeline_logging import exit_with_error

from . import local


def run_configuration(args: PipelineConfiguration) -> None:
    logs_root = args.runs_path
    try:
        acquire_lock(command_lock_path(args, logs_root))
    except RuntimeError as error:
        exit_with_error(error)

    local.run_configuration(args, logs_root)
