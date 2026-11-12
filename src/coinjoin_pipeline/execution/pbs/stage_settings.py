"""The settings.json of an S3 PBS job: the fields every job has and those of each job.

templates_s3.render_python_stage writes the file; the worker on the node reads it.
Standard library only and Python 3.8 syntax, like every module a job ships.
"""

from __future__ import annotations

from typing import TypedDict


class _OutputDirectory(TypedDict):
    # Run-relative directory uploaded after the job.
    relative: str


class OutputSettings(_OutputDirectory, total=False):
    """Where an analysis over an index writes."""


class InputSettings(TypedDict):
    """One download before an analysis: a run directory."""

    run: str


class _AnalysisIO(TypedDict):
    output: OutputSettings
    inputs: list[InputSettings]
    # A run-relative file the work must leave, and the failure message when it did not.
    output_check: list[str] | None


class AnalysisModeSettings(_AnalysisIO, total=False):
    """What detection needs (analysis_modes)."""


class _CommonSettings(TypedDict):
    stage: str
    # The worker function in JOBS; several jobs share a stage name and its markers.
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

    # Every job but coinjoin-mappings runs one image.
    image: str
    # The configured image name; index manifests record it so a rebuilt tag still matches.
    image_name: str


class CommandSettings(TypedDict):
    """The coinjoin-analysis baseline's settings, and the start of most others."""

    # The shell command run in the image.
    command: str


class UnifiedReportSettings(CommandSettings):
    # Run directories the report reads.
    downloads: list[str]


class MappingsSettings(TypedDict):
    """The shell shared with the shared-storage job, and its images."""

    script: str
    enumerator_image: str
    sake_image: str


class BlocksciSettings(CommandSettings):
    """The combined parse and analysis of an emulation."""

    include_report: bool
    export_analysis: bool


class BlocksciAnalysisSettings(CommandSettings, AnalysisModeSettings):
    """Detection over this run's index."""
