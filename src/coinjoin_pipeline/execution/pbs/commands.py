"""In-container command builders for PBS stages."""

from __future__ import annotations

import shlex
from typing import Literal

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.pbs_settings import pbs_image
from coinjoin_pipeline.registry import pinned_reference

from .defaults import BLOCKSCI_IMAGE_PYTHON_COMMAND
from .validation import require_safe_image


def blocksci_pbs_command(run_id: str, config: PipelineConfiguration, include_report: bool = True) -> str:
    command = worker_command("run", run_id, config)
    if include_report:
        command += " --report"
    return command


def blocksci_export_pbs_command(
    run_id: str,
    config: PipelineConfiguration,
    uploader_image: str | None = None,
    unified_report_image: str | None = None,
) -> str:
    command = worker_command("report", run_id, config)
    for flag, image in (
        ("--uploader-image", uploader_image),
        ("--unified-report-image", unified_report_image),
    ):
        if image:
            require_safe_image(image, f"{flag} value")
            command += f" {flag} {shlex.quote(image)}"
    return command


def coinjoin_analysis_pbs_command(action: str = "collect_docker") -> str:
    """Build the in-container command for the coinjoin-analysis PBS stage."""
    return f"python -m cj_process.parse_cj_logs --action {action} --target-path /runs/emulation/selected"


def detector_arguments(config: PipelineConfiguration) -> list[str]:
    """Render detector settings once for analysis and report workers."""
    arguments: list[str] = []
    for name, value in (
        ("coinjoin-type", config.coinjoin_type),
        ("min-input-count", config.min_input_count if config.min_input_count is not None else "default"),
        ("joinmarket-detector", config.joinmarket.detector),
        ("joinmarket-min-base-fee", config.joinmarket.min_base_fee),
        ("joinmarket-percentage-fee", config.joinmarket.percentage_fee),
        ("joinmarket-max-depth", config.joinmarket.max_depth),
    ):
        arguments.extend(["--" + name, str(value)])
    return arguments


def analysis_image_arguments(config: PipelineConfiguration) -> list[str]:
    """Name the images of this run for the analysis artifact's provenance.

    ``singularity exec --cleanenv`` drops the image variables the local
    container path relies on, so the PBS worker receives them as options.
    """
    arguments: list[str] = []
    for flag, image in (
        # The same pinned references the jobs run, so the artifact carries their digests.
        ("--blocksci-image", pinned_reference(pbs_image(config, "blocksci"))),
        ("--coinjoin-analysis-image", pinned_reference(pbs_image(config, "coinjoin_analysis"))),
        ("--coinjoin-emulator-image", config.images.emulator),
        ("--uploader-image", config.images.uploader),
    ):
        if image:
            require_safe_image(image, f"{flag} value")
            arguments.extend([flag, image])
    return arguments


def worker_command(
    phase: Literal["run", "report"],
    run_id: str,
    config: PipelineConfiguration,
) -> str:
    arguments = [
        "/mnt/exporters/worker.py",
        phase,
        "--run-dir",
        f"/runs/emulation/logs/{run_id}",
        *detector_arguments(config),
    ]
    if phase != "report":
        arguments.extend(analysis_image_arguments(config))
    python = "python3" if phase == "report" else BLOCKSCI_IMAGE_PYTHON_COMMAND
    return python + " " + shlex.join(arguments)
