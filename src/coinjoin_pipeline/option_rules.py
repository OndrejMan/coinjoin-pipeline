"""Cross-field policy shared by YAML and CLI configuration inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .configuration import cli_flag, config_value, option_specs
from .storage.s3 import RUN_ID_RULE, is_valid_run_id

if TYPE_CHECKING:
    from .configuration import PipelineConfiguration


def _supplied(config: PipelineConfiguration, path: str, value: object) -> bool:
    """Explicit false/null is inactive; zero and empty strings remain supplied."""
    return path in config.provided and value is not None and value is not False and value != ()


def cross_option_errors(config: PipelineConfiguration) -> list[str]:
    """Return every violated rule in order, retaining CLI spellings in diagnostics."""
    return _action_errors(config) + _general_errors(config)


def _general_errors(config: PipelineConfiguration) -> list[str]:
    """Rules that hold for every action."""
    errors: list[str] = []
    for path, destination, item in option_specs():
        permitted = item.metadata.get("actions")
        if permitted and path in config.provided and config.action not in permitted:
            value = config_value(config, path)
            if value is not None and value is not False:
                errors.append(f"{cli_flag(destination)} is not supported for {config.action}")
    return errors


def _action_errors(config: PipelineConfiguration) -> list[str]:
    """Rules of the emulation and analysis actions."""
    return _run_selection_errors(config)


def _run_selection_errors(config: PipelineConfiguration) -> list[str]:
    errors: list[str] = []
    action = config.action
    if config.run_dir is not None and config.all_runs:
        errors.append("--run-dir and --all-runs are mutually exclusive")
    if action in {"analyze", "export", "coinjoin-analysis"}:
        if not _supplied(config, "run_dir", config.run_dir) and not _supplied(config, "all_runs", config.all_runs):
            errors.append(f"{action} requires --run-dir (or --all-runs where supported)")
    if (
        action == "clean"
        and not _supplied(config, "dry_run", config.dry_run)
        and not _supplied(config, "yes", config.yes)
    ):
        errors.append("clean is destructive; pass --yes or --dry-run")
    if config.run_id is not None and not is_valid_run_id(config.run_id):
        errors.append(f"--run-id {RUN_ID_RULE}")
    return errors
