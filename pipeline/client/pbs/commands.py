"""In-container command builders for PBS stages."""

from __future__ import annotations

from client.artifacts import shell_assignment

from .defaults import BLOCKSCI_IMAGE_PYTHON_COMMAND
from .validation import PBSError, require_safe_image


def blocksci_pbs_command(
    run_id: str,
    coinjoin_type: str,
    min_input_count: int | None,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
    markdown: bool = True,
    include_report: bool = True,
    export_analysis: bool = False,
    blocksci_script: str | None = None,
) -> str:
    """Build the in-container command for the BlockSci PBS stage.

    This runs the existing unified_report.py exporter with the same arguments
    used by the Docker-compose path, but inside Singularity on the compute node.
    """
    config_path = f"/runs/emulation/logs/{run_id}/blocksci_data/config.json"
    parsed_path = f"/runs/emulation/logs/{run_id}/blocksci_data/parsed"
    run_dir_container = f"/runs/emulation/logs/{run_id}"
    parts = [
        "blocksci_parser {config} generate-config bitcoin_regtest {parsed} "
        "--disk /mnt/data/regtest --max-block $((EXPORTED_MAX_BLOCK + 1))",
        "blocksci_parser {config} update",
    ]
    if blocksci_script:
        parts.append(
            "ACTIVE_RUN_ID={run_id} BLOCKSCI_CONFIG={config} "
            "BLOCKSCI_RUN_DIR={run_dir_container} "
            f"{BLOCKSCI_IMAGE_PYTHON_COMMAND} "
            "{blocksci_script}"
        )
    if export_analysis:
        parts.append(
            f"{BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/exporters/blocksci_export/analysis.py "
            "--config {config} "
            "--run-dir {run_dir_container} "
            "--coinjoin-type {coinjoin_type} "
            "--min-input-count {min_input_count} "
            "--joinmarket-detector {joinmarket_detector} "
            "--joinmarket-min-base-fee {joinmarket_min_base_fee} "
            "--joinmarket-percentage-fee {joinmarket_percentage_fee} "
            "--joinmarket-max-depth {joinmarket_max_depth}"
        )
    if include_report:
        parts.append(
            f"{BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/exporters/unified_report.py "
            "--config {config} "
            "--runs-root /runs/emulation/logs "
            "--run-dir {run_dir_container} "
            "--coinjoin-type {coinjoin_type} "
            "--min-input-count {min_input_count} "
            "--joinmarket-detector {joinmarket_detector} "
            "--joinmarket-min-base-fee {joinmarket_min_base_fee} "
            "--joinmarket-percentage-fee {joinmarket_percentage_fee} "
            "--joinmarket-max-depth {joinmarket_max_depth}",
        )
    if include_report and markdown:
        parts[-1] += " --markdown"
    return " && ".join(parts).format(
        config=config_path,
        parsed=parsed_path,
        run_dir_container=run_dir_container,
        coinjoin_type=coinjoin_type,
        min_input_count=min_input_count if min_input_count is not None else "default",
        joinmarket_detector=joinmarket_detector,
        joinmarket_min_base_fee=joinmarket_min_base_fee,
        joinmarket_percentage_fee=joinmarket_percentage_fee,
        joinmarket_max_depth=joinmarket_max_depth,
        run_id=run_id,
        blocksci_script=blocksci_script,
    )


def blocksci_parse_pbs_command(
    run_id: str,
    *,
    coin_type: str = "bitcoin_regtest",
    disk_path: str = "/mnt/data/regtest",
    max_block_expression: str = "$((EXPORTED_MAX_BLOCK + 1))",
) -> str:
    """Build the parser-only command used by the reusable S3 workflow."""
    config_path = f"/runs/emulation/logs/{run_id}/blocksci_data/config.json"
    parsed_path = f"/runs/emulation/logs/{run_id}/blocksci_data/parsed"
    return " && ".join(
        (
            f"blocksci_parser {config_path} generate-config {coin_type} {parsed_path} "
            f"--disk {disk_path} --max-block {max_block_expression}",
            f"blocksci_parser {config_path} update",
        )
    )


def blocksci_update_pbs_command(run_id: str) -> str:
    """Build the parser command for an extracted, rewritten S3 cache."""
    config_path = f"/runs/emulation/logs/{run_id}/blocksci_data/config.json"
    return f"blocksci_parser {config_path} update"


def blocksci_analysis_pbs_command(
    run_id: str,
    coinjoin_type: str,
    min_input_count: int | None,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
) -> str:
    """Build detector analysis over an already parsed BlockSci index."""
    config = f"/runs/emulation/logs/{run_id}/blocksci_data/config.json"
    run_dir = f"/runs/emulation/logs/{run_id}"
    command = (
        f"{BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/exporters/blocksci_export/analysis.py "
        f"--config {config} --run-dir {run_dir} "
        f"--coinjoin-type {coinjoin_type} "
        f"--min-input-count {min_input_count if min_input_count is not None else 'default'} "
        f"--joinmarket-detector {joinmarket_detector} "
        f"--joinmarket-min-base-fee {joinmarket_min_base_fee} "
        f"--joinmarket-percentage-fee {joinmarket_percentage_fee} "
        f"--joinmarket-max-depth {joinmarket_max_depth}"
    )
    return command


def blocksci_external_report_pbs_command(
    run_id: str,
    coinjoin_type: str,
    min_input_count: int | None,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
) -> str:
    """Build a mainnet report against a Dumplings baseline over cached BlockSci."""
    run_dir = f"/runs/emulation/logs/{run_id}"
    config = f"{run_dir}/blocksci_data/config.json"
    return (
        f"{BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/exporters/unified_report.py "
        f"--config {config} --runs-root /runs/emulation/logs --run-dir {run_dir} "
        "--mode external --network bitcoin "
        f"--coinjoin-type {coinjoin_type} "
        f"--min-input-count {min_input_count if min_input_count is not None else 'default'} "
        f"--joinmarket-detector {joinmarket_detector} "
        f"--joinmarket-min-base-fee {joinmarket_min_base_fee} "
        f"--joinmarket-percentage-fee {joinmarket_percentage_fee} "
        f"--joinmarket-max-depth {joinmarket_max_depth} --markdown"
    )


def blocksci_script_pbs_command(
    run_id: str,
    coinjoin_type: str,
    min_input_count: int | None,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
) -> str:
    """Build custom-script execution over an already parsed BlockSci index."""
    run_dir = f"/runs/emulation/logs/{run_id}"
    config = f"{run_dir}/blocksci_data/config.json"
    output = f"{run_dir}/blocksci-custom-analysis_data"
    environment = {
        "ACTIVE_RUN_ID": run_id,
        "BLOCKSCI_CONFIG": config,
        "BLOCKSCI_RUN_DIR": run_dir,
        "BLOCKSCI_OUTPUT_DIR": output,
        "COINJOIN_TYPE": coinjoin_type,
        "JOINMARKET_DETECTOR": joinmarket_detector,
        "JOINMARKET_MIN_BASE_FEE": str(joinmarket_min_base_fee),
        "JOINMARKET_PERCENTAGE_FEE": str(joinmarket_percentage_fee),
        "JOINMARKET_MAX_DEPTH": str(joinmarket_max_depth),
    }
    if min_input_count is not None:
        environment["MIN_INPUT_COUNT"] = str(min_input_count)
    assignments = " ".join(
        shell_assignment(name, value) for name, value in environment.items()
    )
    return f"{assignments} {BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/user-analysis.py"


def blocksci_notebook_pbs_command(notebook_port: int) -> str:
    """Build notebook execution without rebuilding or reparsing BlockSci."""
    if isinstance(notebook_port, bool) or not isinstance(notebook_port, int) or not (1024 <= notebook_port <= 65535):
        raise PBSError("BlockSci notebook port must be between 1024 and 65535")
    return (
        "cd /mnt/blocksci/Notebooks && uv run jupyter notebook --no-browser --ip=0.0.0.0 "
        f"--port={notebook_port} --allow-root --notebook-dir=/mnt/notebooks"
    )


def blocksci_export_pbs_command(
    run_id: str,
    coinjoin_type: str,
    min_input_count: int | None,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
    uploader_image: str | None = None,
    unified_report_image: str | None = None,
) -> str:
    """Build the lightweight report command used after parallel analysis stages."""
    run_dir = f"/runs/emulation/logs/{run_id}"
    analysis = f"{run_dir}/blocksci-analysis_data/blocksci_analysis.json"
    command = (
        "python3 /mnt/exporters/unified_report.py "
        f"--runs-root /runs/emulation/logs --run-dir {run_dir} "
        f"--blocksci-analysis {analysis} "
        f"--coinjoin-type {coinjoin_type} "
        f"--min-input-count {min_input_count if min_input_count is not None else 'default'} "
        f"--joinmarket-detector {joinmarket_detector} "
        f"--joinmarket-min-base-fee {joinmarket_min_base_fee} "
        f"--joinmarket-percentage-fee {joinmarket_percentage_fee} "
        f"--joinmarket-max-depth {joinmarket_max_depth} --markdown"
    )
    # Provenance of the two images that have no environment channel into this
    # job: the uploader that produced the S3 artifacts and the image the report
    # itself runs in. Without them images.uploader/images.unified_report are
    # null in every report, which is what images.wrapper used to record.
    for flag, image in (
        ("--uploader-image", uploader_image),
        ("--unified-report-image", unified_report_image),
    ):
        if image:
            require_safe_image(image, f"{flag} value")
            command += f" {flag} {image}"
    return command


def coinjoin_analysis_pbs_command(action: str = "collect_docker") -> str:
    """Build the in-container command for the coinjoin-analysis PBS stage."""
    return f"python -m cj_process.parse_cj_logs --action {action} --target-path /runs/emulation/selected"
