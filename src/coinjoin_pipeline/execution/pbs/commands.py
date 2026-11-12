"""In-container command builders for PBS stages."""

from __future__ import annotations

import shlex
from typing import Literal

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.pbs_settings import pbs_image

from .defaults import BLOCKSCI_IMAGE_PYTHON_COMMAND
from .validation import require_safe_image


def blocksci_pbs_command(run_id: str, config: PipelineConfiguration, include_report: bool = True) -> str:
    command = worker_command("run", run_id, config)
    if include_report:
        command += " --report"
    return command


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
        ("--blocksci-image", pbs_image(config, "blocksci")),
        ("--coinjoin-emulator-image", config.images.emulator),
        ("--uploader-image", config.images.uploader),
    ):
        if image:
            require_safe_image(image, f"{flag} value")
            arguments.extend([flag, image])
    return arguments


def worker_command(
    phase: Literal["run"],
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
    arguments.extend(analysis_image_arguments(config))
    return BLOCKSCI_IMAGE_PYTHON_COMMAND + " " + shlex.join(arguments)
