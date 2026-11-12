"""The settings.json of an S3 PBS job: the fields every job has and those of each job.

templates_s3.render_python_stage writes the file; the worker on the node reads it.
Standard library only and Python 3.8 syntax, like every module a job ships.
"""

from __future__ import annotations

from typing import TypedDict


class _CommonSettings(TypedDict):
    stage: str
    # The worker function in JOBS that runs the stage.
    job: str
    run_id: str
    run_prefix: str
    endpoint: str
    credentials: str
    profile: str
    log_prefix: str
    # A log the job's whole output goes to, uploaded beside the run's logs.
    job_log: str
    ncpus: int


class StageSettings(_CommonSettings, total=False):
    """settings.json, written by templates_s3.render_python_stage for one job.

    It holds these fields and, in the same object, those of the job's own settings type
    below; each job function reads the latter through ``cast``.
    """

    # The image the job runs.
    image: str


class CommandSettings(TypedDict):
    """The coinjoin-analysis baseline's settings, and the start of most others."""

    # The shell command run in the image.
    command: str


class BlocksciSettings(CommandSettings):
    """The combined parse and analysis of an emulation."""

    include_report: bool
    export_analysis: bool
