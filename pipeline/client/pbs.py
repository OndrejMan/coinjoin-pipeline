"""PBS job submission for MetaCentrum compute nodes.

The frontend (where ``runIt.sh`` runs) only submits PBS jobs via ``qsub``.
The actual BlockSci/coinjoin-analysis work runs on a MetaCentrum compute
node inside a Singularity container, writing results back into the same
run directory under ``/storage``.

Marker files (``.pbs/<stage>.done`` / ``.pbs/<stage>.failed``) are used to
track completion instead of relying solely on ``qstat``, because once a job
disappears from ``qstat`` we still need to know whether the stage produced
the expected artifacts.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from client.artifacts import (
    render_s5cmd_check,
    render_s5cmd_cp,
    render_s5cmd_sync,
    shell_assignment,
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
    validate_s3_profile,
)

DEFAULT_BLOCKSCI_NCPUS = 8
DEFAULT_BLOCKSCI_MEM = "64gb"
DEFAULT_BLOCKSCI_SCRATCH = "100gb"
DEFAULT_BLOCKSCI_WALLTIME = "24:00:00"

DEFAULT_COINJOIN_ANALYSIS_NCPUS = 4
DEFAULT_COINJOIN_ANALYSIS_MEM = "16gb"
DEFAULT_COINJOIN_ANALYSIS_SCRATCH = "50gb"
DEFAULT_COINJOIN_ANALYSIS_WALLTIME = "04:00:00"

DEFAULT_BLOCKSCI_IMAGE = "docker://ghcr.io/ondrejman/blocksci-complete:latest"
DEFAULT_COINJOIN_ANALYSIS_IMAGE = "docker://ghcr.io/ondrejman/coinjoin-analysis:latest"
DEFAULT_MAPPINGS_ENUMERATOR_IMAGE = "docker://ghcr.io/ondrejman/coinjoin-mappings-enumerator:latest"
DEFAULT_SAKE_IMAGE = "docker://ghcr.io/ondrejman/coinjoin-mappings-sake:latest"

POLL_INTERVAL_SECONDS = 30


class PBSError(RuntimeError):
    """Raised when PBS submission or execution fails."""


def require_qsub() -> None:
    """Ensure ``qsub`` is available; this must run on a MetaCentrum frontend."""
    if shutil.which("qsub") is None:
        raise PBSError("PBS stages must be run on a MetaCentrum frontend with qsub available")


def require_storage_path(run_dir: Path) -> None:
    """PBS jobs need the run directory on shared MetaCentrum storage (/storage)."""
    resolved = str(run_dir.resolve())
    if not resolved.startswith("/storage/"):
        raise PBSError(f"PBS jobs need run-dir on shared MetaCentrum storage (/storage), not: {resolved}")


def require_existing_path(path: Path, description: str) -> None:
    """Ensure a path used by the PBS job exists before submitting."""
    if not path.exists():
        raise PBSError(f"{description} does not exist: {path}")


def require_bitcoin_datadir(path: Path) -> None:
    """Ensure the supplied Bitcoin Core datadir has the shape BlockSci expects."""
    require_existing_path(path, "PBS Bitcoin datadir")
    if not (path / "regtest" / "blocks").is_dir():
        raise PBSError(f"PBS Bitcoin datadir must contain regtest/blocks so BlockSci can read it: {path}")


def render_blocksci_pbs(
    run_dir: Path,
    logs_root: Path,
    bitcoin_datadir: Path,
    exporters_dir: Path,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
    stage: str = "blocksci",
    job_name: str = "blocksci_analysis",
) -> str:
    """Render a PBS script for the BlockSci analysis stage."""
    template = (Path(__file__).parent / "blocksci_template.sh").read_text(encoding="utf-8")
    return template.format(
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        run_dir=run_dir,
        logs_root=logs_root,
        bitcoin_datadir=bitcoin_datadir,
        exporters_dir=exporters_dir,
        image=image,
        blocksci_command=command,
        stage=stage,
        job_name=job_name,
    )


def render_coinjoin_analysis_pbs(
    run_dir: Path,
    output_dir: Path,
    input_data_dir: Path,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
) -> str:
    """Render a PBS script for the coinjoin-analysis stage."""
    template = (Path(__file__).parent / "coinjoin_analysis_template.sh").read_text(encoding="utf-8")
    return template.format(
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        run_dir=run_dir,
        output_dir=output_dir,
        input_data_dir=input_data_dir,
        image=image,
        coinjoin_analysis_command=command,
    )


def render_mappings_pbs(
    run_dir: Path,
    enumerator_image: str,
    sake_image: str,
    *,
    mining_fee_rate: int = 1,
    coordination_fee_rate: float = 0.003,
    max_decomposition_fee: int = 6000,
    mode: str = "numeric",
    timeout: int = 60,
    retry_timeout: int = 600,
    sake_seed: int = 20260704,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
) -> str:
    template = (Path(__file__).parent / "mappings_template.sh").read_text(encoding="utf-8")
    return template.format(
        run_dir=run_dir,
        enumerator_image=enumerator_image,
        sake_image=sake_image,
        mining_fee_rate=mining_fee_rate,
        coordination_fee_rate=coordination_fee_rate,
        max_decomposition_fee=max_decomposition_fee,
        mode=mode,
        timeout=timeout,
        retry_timeout=retry_timeout,
        sake_seed=sake_seed,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )


def submit_pbs(script_path: Path, dependency_job_id: str | None = None) -> str:
    """Submit a PBS script via ``qsub`` and return the job ID."""
    command = ["qsub"]
    if dependency_job_id:
        command.extend(["-W", f"depend=afterok:{dependency_job_id}"])
    command.append(str(script_path))
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise PBSError(f"qsub failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def _s3_values(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
) -> dict[str, str]:
    return {
        "artifact_uri": shell_assignment("ARTIFACT_URI", validate_artifact_uri(artifact_uri)).split("=", 1)[1],
        "run_id": shell_assignment("RUN_ID", validate_run_id(run_id)).split("=", 1)[1],
        "endpoint_url": shell_assignment("S3_ENDPOINT_URL", validate_s3_endpoint_url(endpoint_url)).split("=", 1)[1],
        "credentials_file": shell_assignment("S3_CREDENTIALS_FILE", validate_credentials_file(credentials_file)).split(
            "=", 1
        )[1],
        "profile": shell_assignment("S3_PROFILE", validate_s3_profile(profile)).split("=", 1)[1],
    }


def render_coinjoin_analysis_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
) -> str:
    values = _s3_values(artifact_uri, run_id, endpoint_url, credentials_file, profile)
    template = (Path(__file__).parent / "coinjoin_analysis_s3_template.sh").read_text(encoding="utf-8")
    return template.format(
        **values,
        image=shell_assignment("IMAGE", image).split("=", 1)[1],
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        s5cmd_check=render_s5cmd_check(),
        download_run=render_s5cmd_sync('"$ARTIFACT_URI/$RUN_ID/*"', '"$RUN_WORK/"'),
        upload_results=render_s5cmd_sync(
            '"$RUN_WORK/coinjoin-analysis_data/"', '"$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/"'
        ),
        upload_failed=render_s5cmd_cp('"$FAILED_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/coinjoin-analysis.failed"'),
        upload_done=render_s5cmd_cp('"$DONE_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/coinjoin-analysis.done"'),
    )


def render_blocksci_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
) -> str:
    values = _s3_values(artifact_uri, run_id, endpoint_url, credentials_file, profile)
    template = (Path(__file__).parent / "blocksci_s3_template.sh").read_text(encoding="utf-8")
    return template.format(
        **values,
        image=shell_assignment("IMAGE", image).split("=", 1)[1],
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        s5cmd_check=render_s5cmd_check(),
        download_run=render_s5cmd_sync('"$ARTIFACT_URI/$RUN_ID/*"', '"$RUN_WORK/"'),
        upload_blocksci=render_s5cmd_sync('"$RUN_WORK/blocksci_data/"', '"$ARTIFACT_URI/$RUN_ID/blocksci_data/"'),
        upload_report=render_s5cmd_sync(
            '"$RUN_WORK/blocksciEmulatorAnalysis_data/"', '"$ARTIFACT_URI/$RUN_ID/blocksciEmulatorAnalysis_data/"'
        ),
        upload_logs=render_s5cmd_sync('"$RUN_WORK/logs/"', '"$ARTIFACT_URI/$RUN_ID/logs/"'),
        upload_failed=render_s5cmd_cp('"$FAILED_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci.failed"'),
        upload_done=render_s5cmd_cp('"$DONE_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci.done"'),
    )


def _submit_s3_script(
    script: str,
    stage: str,
    dry_run: bool,
    dependency_job_id: str | None = None,
) -> str | None:
    if dry_run:
        print(f"[dry-run] PBS S3-compatible script for {stage}:\n{script}")
        return None
    require_qsub()
    with tempfile.TemporaryDirectory(prefix="coinjoin-pbs-") as directory:
        path = Path(directory)
        path.chmod(0o700)
        script_path = path / f"{stage}.pbs"
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o700)
        job_id = submit_pbs(script_path, dependency_job_id)
    print(f"[pbs] Submitted {stage} S3-compatible PBS job: {job_id}")
    return job_id


def submit_coinjoin_analysis_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
    dry_run: bool = False,
) -> str | None:
    script = render_coinjoin_analysis_s3_pbs(
        artifact_uri,
        run_id,
        endpoint_url,
        credentials_file,
        profile,
        image,
        command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )
    return _submit_s3_script(script, "coinjoin-analysis", dry_run)


def submit_blocksci_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
    dry_run: bool = False,
    dependency_job_id: str | None = None,
) -> str | None:
    script = render_blocksci_s3_pbs(
        artifact_uri,
        run_id,
        endpoint_url,
        credentials_file,
        profile,
        image,
        command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )
    return _submit_s3_script(script, "blocksci", dry_run, dependency_job_id)


def wait_for_pbs_marker(run_dir: Path, stage: str, poll_interval: int = POLL_INTERVAL_SECONDS) -> None:
    """Block until the PBS stage writes a ``.done`` or ``.failed`` marker."""
    done = run_dir / ".pbs" / f"{stage}.done"
    failed = run_dir / ".pbs" / f"{stage}.failed"

    while True:
        if failed.exists():
            raise PBSError(f"PBS stage failed: {stage}")
        if done.exists():
            return
        time.sleep(poll_interval)


def submit_blocksci_pbs(
    run_dir: Path,
    logs_root: Path,
    bitcoin_datadir: Path,
    exporters_dir: Path,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
    dry_run: bool = False,
    stage: str = "blocksci",
    job_name: str = "blocksci_analysis",
) -> str | None:
    """Submit a BlockSci PBS job; returns job ID (or None if dry-run)."""
    require_storage_path(run_dir)
    require_storage_path(logs_root)
    require_storage_path(bitcoin_datadir)
    require_storage_path(exporters_dir)
    require_existing_path(exporters_dir, "PBS exporters directory")
    require_bitcoin_datadir(bitcoin_datadir)
    script = render_blocksci_pbs(
        run_dir,
        logs_root,
        bitcoin_datadir,
        exporters_dir,
        image,
        command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        stage=stage,
        job_name=job_name,
    )
    script_path = run_dir / ".pbs" / f"{stage}.pbs"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    if dry_run:
        print(f"[dry-run] PBS script for {stage}:\n{script}")
        return None
    require_qsub()
    job_id = submit_pbs(script_path)
    print(f"[pbs] Submitted {stage} PBS job: {job_id}")
    return job_id


def submit_coinjoin_analysis_pbs(
    run_dir: Path,
    output_dir: Path,
    input_data_dir: Path,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
    dry_run: bool = False,
) -> str | None:
    """Submit a coinjoin-analysis PBS job; returns job ID (or None if dry-run)."""
    require_storage_path(run_dir)
    require_storage_path(output_dir)
    require_storage_path(input_data_dir)
    require_existing_path(input_data_dir, "PBS coinjoin-analysis input data directory")
    script = render_coinjoin_analysis_pbs(
        run_dir,
        output_dir,
        input_data_dir,
        image,
        command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )
    script_path = run_dir / ".pbs" / "coinjoin-analysis.pbs"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    if dry_run:
        print(f"[dry-run] PBS script for coinjoin-analysis:\n{script}")
        return None
    require_qsub()
    job_id = submit_pbs(script_path)
    print(f"[pbs] Submitted coinjoin-analysis PBS job: {job_id}")
    return job_id


def submit_mappings_pbs(
    run_dir: Path,
    enumerator_image: str,
    sake_image: str,
    *,
    mining_fee_rate: int = 1,
    coordination_fee_rate: float = 0.003,
    max_decomposition_fee: int = 6000,
    mode: str = "numeric",
    timeout: int = 60,
    retry_timeout: int = 600,
    sake_seed: int = 20260704,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
    dry_run: bool = False,
) -> str | None:
    require_storage_path(run_dir)
    require_existing_path(run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json", "CoinJoin mappings input")
    script = render_mappings_pbs(
        run_dir,
        enumerator_image,
        sake_image,
        mining_fee_rate=mining_fee_rate,
        coordination_fee_rate=coordination_fee_rate,
        max_decomposition_fee=max_decomposition_fee,
        mode=mode,
        timeout=timeout,
        retry_timeout=retry_timeout,
        sake_seed=sake_seed,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )
    script_path = run_dir / ".pbs" / "coinjoin-mappings.pbs"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    if dry_run:
        print(f"[dry-run] PBS script for coinjoin-mappings:\n{script}")
        return None
    require_qsub()
    job_id = submit_pbs(script_path)
    print(f"[pbs] Submitted coinjoin-mappings PBS job: {job_id}")
    return job_id


def blocksci_pbs_command(
    run_id: str,
    coinjoin_type: str,
    min_input_count: int | None,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
    test_values: bool,
    markdown: bool = True,
    include_report: bool = True,
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
            "BLOCKSCI_RUN_DIR={run_dir_container} python3 {blocksci_script}"
        )
    if include_report:
        parts.append(
            "python3 /mnt/exporters/unified_report.py "
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
    if include_report and test_values:
        parts[-1] += " --test-values"
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


def blocksci_export_pbs_command(
    run_id: str,
    coinjoin_type: str,
    min_input_count: int | None,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
    test_values: bool,
) -> str:
    """Build the report-only command used after parallel analysis stages."""
    config = f"/runs/emulation/logs/{run_id}/blocksci_data/config.json"
    run_dir = f"/runs/emulation/logs/{run_id}"
    command = (
        "python3 /mnt/exporters/unified_report.py "
        f"--config {config} --runs-root /runs/emulation/logs --run-dir {run_dir} "
        f"--coinjoin-type {coinjoin_type} "
        f"--min-input-count {min_input_count if min_input_count is not None else 'default'} "
        f"--joinmarket-detector {joinmarket_detector} "
        f"--joinmarket-min-base-fee {joinmarket_min_base_fee} "
        f"--joinmarket-percentage-fee {joinmarket_percentage_fee} "
        f"--joinmarket-max-depth {joinmarket_max_depth} --markdown"
    )
    if test_values:
        command += " --test-values"
    return command


def coinjoin_analysis_pbs_command(action: str = "collect_docker") -> str:
    """Build the in-container command for the coinjoin-analysis PBS stage."""
    return f"python -m cj_process.parse_cj_logs --action {action} --target-path /runs/emulation/selected"
