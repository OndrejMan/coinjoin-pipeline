"""Fixtures for the typed execution API; never render configuration as argv."""

from pathlib import Path
from types import SimpleNamespace

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.configuration import PipelineConfiguration


def configuration(**values):
    submission = values.pop("pbs_submission_dir", None)
    if submission is not None:
        values["runs_root"] = str(Path(submission).parent)
        values["run_id"] = Path(submission).name
    values = {key: str(value) if isinstance(value, Path) else value for key, value in values.items()}
    return PipelineConfiguration.from_flat(values)


def build_parser():
    return SimpleNamespace(parse_args=load_configuration)
