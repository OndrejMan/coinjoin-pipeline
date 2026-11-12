"""Dispatch a resolved configuration to the experimental local paths in ``local.py``."""

from __future__ import annotations

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.locks import acquire_lock, command_lock_path
from coinjoin_pipeline.execution.pipeline_logging import exit_with_error

from . import local


def _print_dry_run(args: PipelineConfiguration) -> bool:
    """Print dry-run intent and return whether dispatch must continue."""
    if not args.dry_run:
        return True
    print(f"[dry-run] action: {args.action}")
    print(f"[dry-run] runtime: {args.runtime}")
    print(f"[dry-run] engine: {args.engine}")
    print("[dry-run] No containers, files or reports will be created.")
    return False


def run_configuration(args: PipelineConfiguration) -> None:
    if not _print_dry_run(args):
        return
    logs_root = args.runs_path
    if not args.dry_run:
        try:
            acquire_lock(command_lock_path(args, logs_root))
        except RuntimeError as error:
            exit_with_error(error)

    local.run_configuration(args, logs_root)
