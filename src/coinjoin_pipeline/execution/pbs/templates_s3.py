"""S3-backed PBS job renderers.

Every job is ``directives`` and a few lines of shell that write the stage worker's
modules (``scripts.STAGE_RUNTIME_MODULES``) and a ``settings.json`` to the node's
scratch and run them. The renderers here validate the options and assemble those
settings; the work itself is ``analyzer_jobs``' and ``blocksci_jobs``'.
"""

from __future__ import annotations

import json
from typing import Union

from exporters.artifact_paths import (
    BLOCKSCI_ANALYSIS_DIR,
    COINJOIN_ANALYSIS_DIR,
    EMULATOR_DIR,
    MAPPINGS_DIR,
    STAGED_EXPORTERS_DIR,
)

from coinjoin_pipeline.configuration import MappingsConfiguration
from coinjoin_pipeline.registry import pinned_reference
from coinjoin_pipeline.storage.s3 import (
    S3Target,
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
    validate_s3_profile,
)

from .defaults import DEFAULT_RESOURCES, PBSResources
from .scripts import (
    STAGE_RUNTIME_MODULES,
    directives,
    mappings_run,
    launcher,
    package_files,
    write_files,
)
from .stage_settings import (
    BlocksciSettings,
    CommandSettings,
    MappingsSettings,
    StageSettings,
    UnifiedReportSettings,
)
from .validation import (
    require_safe_image,
    require_safe_pbs_token,
)

BLOCKSCI = DEFAULT_RESOURCES["blocksci"]
# The fields one job adds to the common StageSettings; its job function reads them.
JobSettings = Union[
    CommandSettings,
    UnifiedReportSettings,
    MappingsSettings,
    BlocksciSettings,
]


def render_coinjoin_analysis_s3_pbs(
    target: S3Target, image: str, command: str, resources: PBSResources = DEFAULT_RESOURCES["analysis"]
) -> str:
    """Render the baseline job that runs coinjoin-analysis over the emulator logs."""
    return render_python_stage("coinjoin-analysis", target, resources, image, {"command": command})


def render_mappings_s3_pbs(
    target: S3Target,
    enumerator_image: str,
    sake_image: str,
    settings: MappingsConfiguration = MappingsConfiguration(),
    resources: PBSResources = DEFAULT_RESOURCES["mappings"],
) -> str:
    """Render the Wasabi mappings/Sake stage over S3-backed inputs."""
    require_safe_image(enumerator_image, "enumerator image")
    require_safe_image(sake_image, "Sake image")
    return render_python_stage(
        "coinjoin-mappings",
        target,
        resources,
        None,
        {"script": mappings_run(settings), "enumerator_image": enumerator_image, "sake_image": sake_image},
    )


def render_blocksci_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    include_report: bool = True,
    export_analysis: bool = False,
) -> str:
    """Parse and analyze in one job."""
    return render_python_stage(
        "blocksci",
        target,
        resources,
        image,
        {"command": command, "include_report": include_report, "export_analysis": export_analysis},
    )


def render_python_stage(
    stage: str,
    target: S3Target,
    resources: PBSResources,
    image: str | None,
    settings: JobSettings,
) -> str:
    """The job script: write the worker's modules and settings.json to scratch and run ``stage_runtime``.

    The worker publishes the job's terminal marker; only an absent SCRATCHDIR or python3
    ends the job before it can.
    """
    require_safe_pbs_token(stage, "PBS stage")
    common: StageSettings = {
        "stage": stage,
        "job": stage,
        "run_id": validate_run_id(target.run_id),
        "run_prefix": validate_artifact_uri(target.run_prefix),
        "endpoint": validate_s3_endpoint_url(target.endpoint_url),
        "credentials": validate_credentials_file(target.credentials_file),
        "profile": validate_s3_profile(target.profile),
        "log_prefix": target.log_prefix,
        # The whole output goes to S3, so no stage needs PBS to copy its output file back.
        "job_log": f"{stage}.pbs.log",
        "ncpus": resources.ncpus,
    }
    image_settings: dict[str, str] = {}
    if image is not None:
        require_safe_image(image)
        # The job runs the digest the tag names now.
        image_settings = {"image": pinned_reference(image)}
    files = {
        # One flat object: the worker reads it as StageSettings and as the job's own type.
        "settings.json": json.dumps({**common, **settings, **image_settings}, indent=2),
        "run.py": launcher("coinjoin_pipeline.execution.pbs.stage_runtime"),
        **package_files(*STAGE_RUNTIME_MODULES),
    }
    return "\n".join(
        (
            directives(stage.replace("-", "_") + "_s3", resources),
            "set -euo pipefail",
            'test -n "${SCRATCHDIR:-}" || { echo "SCRATCHDIR is not set" >&2; exit 1; }',
            'JOB="$SCRATCHDIR/.stage-runtime"',
            'mkdir -p "$JOB"',
            write_files("$JOB", files),
            # -s: a coinjoin_pipeline installed in the user's site-packages never shadows the shipped one.
            'exec python3 -s "$JOB/run.py" "$JOB/settings.json"',
            "",
        )
    )


def render_unified_report_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = DEFAULT_RESOURCES["unified_report"],
    *,
    include_mappings: bool = False,
) -> str:
    """Render the S3 report-only job that joins both analyzer outputs."""
    downloads = [
        STAGED_EXPORTERS_DIR,
        EMULATOR_DIR,
        COINJOIN_ANALYSIS_DIR,
        BLOCKSCI_ANALYSIS_DIR,
        *((MAPPINGS_DIR,) if include_mappings else ()),
    ]
    return render_python_stage("unified-report", target, resources, image, {"command": command, "downloads": downloads})
