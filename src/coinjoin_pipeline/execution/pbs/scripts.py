"""Shared pieces of every rendered PBS job script."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from coinjoin_pipeline.configuration import MappingsConfiguration
from coinjoin_pipeline.storage.s3 import validate_artifact_uri

from .defaults import PBSResources
from .validation import PBSError, require_safe_pbs_resources

TEMPLATES = Path(__file__).parent / "templates"


def template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def directives(job_name: str, resources: PBSResources, multijob: str | None = None) -> str:
    """The ``#PBS`` header: one chunk with the requested resources, output joined to the job log."""
    require_safe_pbs_resources(resources, multijob)
    select = (
        f"select=1:ncpus={resources.ncpus}:mem={resources.mem}:scratch_{resources.scratch_type}={resources.scratch}"
    )
    return "\n".join(
        (
            "#!/bin/bash",
            f"#PBS -N {job_name}",
            f"#PBS -l {select}{f':multijob={multijob}' if multijob else ''}",
            f"#PBS -l walltime={resources.walltime}",
            "#PBS -j oe",
        )
    )


def mappings_run(settings: MappingsConfiguration) -> str:
    """The enumerator and Sake run shared by the S3 and the shared-storage mappings jobs."""
    return template("mappings_run.sh").format(**asdict(settings))


def validated_s3_uri(value: str, description: str) -> str:
    try:
        return validate_artifact_uri(value)
    except ValueError as error:
        raise PBSError(f"Invalid {description}: {error}") from error


def download_run_directory(name: str) -> str:
    """Sync one run directory from the run prefix into the job's run work tree."""
    return f's5 sync "$ARTIFACT_URI/$RUN_ID/{name}/*" "$RUN_WORK/{name}/"'
