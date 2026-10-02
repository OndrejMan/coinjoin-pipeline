"""In-container command builders for PBS stages."""

from __future__ import annotations

import shlex
from typing import Literal

from exporters.artifact_paths import BLOCKSCI_CONFIG_FILE, BLOCKSCI_CUSTOM_ANALYSIS_DIR

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.storage.s3 import shell_assignment

from .defaults import BLOCKSCI_IMAGE_PYTHON_COMMAND
from .validation import PBSError, require_safe_image


def blocksci_pbs_command(
    run_id: str,
    config: PipelineConfiguration,
    include_report: bool = True,
    blocksci_script: str | None = None,
) -> str:
    command = worker_command("run", run_id, config)
    if blocksci_script:
        command += " --script " + shlex.quote(blocksci_script)
    if include_report:
        command += " --report"
    return command


def blocksci_parse_pbs_command(
    run_id: str,
    *,
    coin_type: str = "bitcoin_regtest",
    disk_path: str = "/mnt/data/regtest",
    max_block_expression: str = "$((PBS_EXPORTED_MAX_BLOCK + 1))",
) -> str:
    return (
        f"{BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/exporters/worker.py parse "
        f"--run-dir {shlex.quote('/runs/emulation/logs/' + run_id)} "
        f"--network {shlex.quote(coin_type)} --disk {shlex.quote(disk_path)} "
        f"--max-block {max_block_expression}"
    )


def blocksci_update_pbs_command(run_id: str) -> str:
    return (
        f"{BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/exporters/worker.py update "
        f"--run-dir {shlex.quote('/runs/emulation/logs/' + run_id)}"
    )


def blocksci_analysis_pbs_command(
    run_id: str,
    config: PipelineConfiguration,
) -> str:
    return worker_command("analyze", run_id, config)


def blocksci_external_report_pbs_command(
    run_id: str,
    config: PipelineConfiguration,
) -> str:
    return worker_command("analyze", run_id, config) + " --mode external --network bitcoin --skip-clustering --report"


def blocksci_script_pbs_command(
    run_id: str,
    config: PipelineConfiguration,
) -> str:
    """Build custom-script execution over an already parsed BlockSci index."""
    run_dir = f"/runs/emulation/logs/{run_id}"
    config_path = f"{run_dir}/{BLOCKSCI_CONFIG_FILE}"
    output = f"{run_dir}/{BLOCKSCI_CUSTOM_ANALYSIS_DIR}"
    environment = {
        "ACTIVE_RUN_ID": run_id,
        "BLOCKSCI_CONFIG": config_path,
        "BLOCKSCI_RUN_DIR": run_dir,
        "BLOCKSCI_OUTPUT_DIR": output,
        "COINJOIN_TYPE": config.coinjoin_type,
        "JOINMARKET_DETECTOR": config.joinmarket.detector,
        "JOINMARKET_MIN_BASE_FEE": str(config.joinmarket.min_base_fee),
        "JOINMARKET_PERCENTAGE_FEE": str(config.joinmarket.percentage_fee),
        "JOINMARKET_MAX_DEPTH": str(config.joinmarket.max_depth),
    }
    if config.min_input_count is not None:
        environment["MIN_INPUT_COUNT"] = str(config.min_input_count)
    assignments = " ".join(shell_assignment(name, value) for name, value in environment.items())
    return f"{assignments} {BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/user-analysis.py"


def blocksci_notebook_pbs_command(notebook_port: int) -> str:
    """Build notebook execution without rebuilding or reparsing BlockSci."""
    if isinstance(notebook_port, bool) or not isinstance(notebook_port, int) or not 1024 <= notebook_port <= 65535:
        raise PBSError("BlockSci notebook port must be between 1024 and 65535")
    return (
        "cd /mnt/blocksci/Notebooks && uv run jupyter notebook --no-browser --ip=0.0.0.0 "
        f"--port={notebook_port} --allow-root --notebook-dir=/mnt/notebooks"
    )


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


def worker_command(
    phase: Literal["run", "analyze", "report"],
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
    python = "python3" if phase == "report" else BLOCKSCI_IMAGE_PYTHON_COMMAND
    return python + " " + shlex.join(arguments)
