"""Fixtures for the typed execution API; never render configuration as argv."""

from pathlib import Path
from types import SimpleNamespace

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.context import resolve_run_images


def configuration(**values):
    """A configuration with its images resolved, as RunContext hands every run one."""
    values = {key: str(value) if isinstance(value, Path) else value for key, value in values.items()}
    return resolve_run_images(PipelineConfiguration.from_flat(values))[0]


def build_parser():
    return SimpleNamespace(parse_args=lambda argv: resolve_run_images(load_configuration(argv))[0])
