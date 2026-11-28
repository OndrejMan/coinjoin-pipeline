"""Fixtures for the typed execution API; never render configuration as argv."""

from pathlib import Path
from types import SimpleNamespace

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.context import resolve_run_images


def configuration(values=None):
    """A configuration from YAML paths with its images resolved, as RunContext hands every run one."""
    values = {key: str(value) if isinstance(value, Path) else value for key, value in (values or {}).items()}
    return resolve_run_images(PipelineConfiguration.from_paths(values))[0]


def build_parser():
    return SimpleNamespace(parse_args=configuration)
