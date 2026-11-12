"""The settings.json of an S3 PBS job: the fields every job has and those of each job.

templates_s3.render_python_stage writes the file; the worker on the node reads it.
Standard library only and Python 3.8 syntax, like every module a job ships.
"""

from __future__ import annotations

from typing import TypedDict


class _SourceKind(TypedDict):
    kind: str
    network: str


class SourceSettings(_SourceKind, total=False):
    """The single block source of a parse or update; ``kind`` becomes the manifest's source_kind."""

    # What a parse logs it is preparing.
    description: str
    # The last height to parse; an imported BlockSci index names its own.
    max_block: int
    # external-bitcoin: the shared Bitcoin Core coin directory.
    datadir: str
    # bitcoin-blocks-s3: the block archive and the hash the parsed chain must end with.
    blocks_uri: str
    expected_hash: str | None
    # external-blocksci: an existing index to import.
    blocksci_dir: str


class _OutputDirectory(TypedDict):
    # Run-relative directory uploaded after the job.
    relative: str


class OutputSettings(_OutputDirectory, total=False):
    """Where an analysis over an index writes."""

    # A notebook writes to the submitting user's home, or to notebooks_dir.
    notebook: bool
    notebooks_dir: str


class InputSettings(TypedDict):
    """One download before an analysis: a run directory."""

    run: str


class _AnalysisIO(TypedDict):
    output: OutputSettings
    inputs: list[InputSettings]
    # A run-relative file the work must leave, and the failure message when it did not.
    output_check: list[str] | None


class AnalysisModeSettings(_AnalysisIO, total=False):
    """What detection, a user script or a notebook needs (analysis_modes)."""

    user_script: str
    connection_help: list[str]


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


class BlocksciParseSettings(CommandSettings):
    source: SourceSettings
    # How the BlockSci image runs verify_chain.py.
    python_command: str


class BlocksciUpdateSettings(BlocksciParseSettings):
    # The run whose S3 cache is extended.
    source_run_id: str | None
    artifact_uri: str


class BlocksciAnalysisSettings(CommandSettings, AnalysisModeSettings):
    # The run whose S3 cache is read, by default this one.
    source_run_id: str | None
    artifact_uri: str
