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

import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from client.artifacts import (
    PROBE_RUNNING,
    PROBE_TERMINAL,
    PROBE_UNKNOWN,
    render_s5cmd_check,
    render_s5cmd_cp,
    render_s5cmd_rm,
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

DEFAULT_UNIFIED_REPORT_NCPUS = 2
DEFAULT_UNIFIED_REPORT_MEM = "8gb"
DEFAULT_UNIFIED_REPORT_SCRATCH = "10gb"
DEFAULT_UNIFIED_REPORT_WALLTIME = "01:00:00"

DEFAULT_BLOCKSCI_IMAGE = "docker://ghcr.io/ondrejman/blocksci-complete:latest"
DEFAULT_COINJOIN_ANALYSIS_IMAGE = "docker://ghcr.io/ondrejman/coinjoin-analysis:latest"
DEFAULT_MAPPINGS_ENUMERATOR_IMAGE = "docker://ghcr.io/ondrejman/coinjoin-mappings-enumerator:latest"
DEFAULT_SAKE_IMAGE = "docker://ghcr.io/ondrejman/coinjoin-mappings-sake:latest"

POLL_INTERVAL_SECONDS = 30
PBS_TERMINAL_STATES = {"C", "F"}
PBS_ACTIVE_STATES = {"B", "E", "H", "M", "Q", "R", "S", "T", "U", "W"}
PBS_QUEUE_MARGIN_SECONDS = 60 * 60


class PBSError(RuntimeError):
    """Raised when PBS submission or execution fails."""


def walltime_to_seconds(walltime: str) -> int:
    """Convert PBS walltime (HH:MM:SS or DD:HH:MM:SS) to seconds."""
    if not isinstance(walltime, str) or not re.fullmatch(r"[0-9]+(?::[0-9]+){2,3}", walltime):
        raise PBSError(f"Unsupported PBS walltime format: {walltime}")
    parts = walltime.split(":")
    if len(parts) == 3:
        days = "0"
        hours, minutes, seconds = parts
    elif len(parts) == 4:
        days, hours, minutes, seconds = parts
    else:
        raise PBSError(f"Unsupported PBS walltime format: {walltime}")
    day_value, hour_value = int(days), int(hours)
    minute_value, second_value = int(minutes), int(seconds)
    if minute_value >= 60 or second_value >= 60 or (len(parts) == 4 and hour_value >= 24):
        raise PBSError(f"Unsupported PBS walltime format: {walltime}")
    total = (((day_value * 24) + hour_value) * 60 + minute_value) * 60 + second_value
    if total <= 0:
        raise PBSError("PBS walltime must be greater than zero")
    return total


def require_qsub() -> None:
    """Ensure ``qsub`` is available; this must run on a MetaCentrum frontend."""
    if shutil.which("qsub") is None:
        raise PBSError("PBS stages must be run on a MetaCentrum frontend with qsub available")


# Paths are rendered into PBS shell templates via str.format; restrict them to
# characters that survive both PBS directives and unquoted shell contexts.
SAFE_TEMPLATE_PATH_RE = re.compile(r"^[A-Za-z0-9/._+:@-]+$")
SAFE_PBS_SIZE_RE = re.compile(r"^[1-9][0-9]*(?:b|kb|mb|gb|tb)$", re.IGNORECASE)
SAFE_IMAGE_RE = re.compile(r"^[A-Za-z0-9/._+:@%=-]+$")
SAFE_PBS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def require_safe_template_path(path: Path, description: str) -> None:
    if not SAFE_TEMPLATE_PATH_RE.fullmatch(str(path)):
        raise PBSError(f"{description} contains characters unsafe for PBS job templates: {path}")


def require_safe_image(image: str, description: str = "container image") -> None:
    """Reject shell metacharacters before interpolating an image into a PBS script."""
    if not isinstance(image, str) or not image or not SAFE_IMAGE_RE.fullmatch(image):
        raise PBSError(f"{description} contains characters unsafe for PBS job templates: {image}")


def require_safe_pbs_resources(ncpus: int, mem: str, scratch: str, walltime: str) -> None:
    """Validate values interpolated into ``#PBS -l`` directives."""
    if isinstance(ncpus, bool) or not isinstance(ncpus, int) or ncpus <= 0:
        raise PBSError("PBS ncpus must be a positive integer")
    if not isinstance(mem, str) or not SAFE_PBS_SIZE_RE.fullmatch(mem):
        raise PBSError(f"Unsupported PBS memory value: {mem}")
    if not isinstance(scratch, str) or not SAFE_PBS_SIZE_RE.fullmatch(scratch):
        raise PBSError(f"Unsupported PBS scratch value: {scratch}")
    walltime_to_seconds(walltime)


def require_safe_pbs_token(value: str, description: str) -> None:
    if not isinstance(value, str) or not value or not SAFE_PBS_TOKEN_RE.fullmatch(value):
        raise PBSError(f"{description} contains characters unsafe for PBS job templates: {value}")


def require_storage_path(run_dir: Path) -> None:
    """PBS jobs need the run directory on shared MetaCentrum storage (/storage)."""
    resolved = str(run_dir.resolve())
    if not resolved.startswith("/storage/"):
        raise PBSError(f"PBS jobs need run-dir on shared MetaCentrum storage (/storage), not: {resolved}")
    require_safe_template_path(run_dir.resolve(), "PBS path")


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
    for path, description in (
        (run_dir, "run directory"),
        (logs_root, "logs root"),
        (bitcoin_datadir, "Bitcoin datadir"),
        (exporters_dir, "exporters directory"),
    ):
        require_safe_template_path(path, description)
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    require_safe_pbs_token(stage, "PBS stage")
    require_safe_pbs_token(job_name, "PBS job name")
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
    for path, description in (
        (run_dir, "run directory"),
        (output_dir, "output directory"),
        (input_data_dir, "input data directory"),
    ):
        require_safe_template_path(path, description)
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
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
    require_safe_template_path(run_dir, "run directory")
    require_safe_image(enumerator_image, "enumerator image")
    require_safe_image(sake_image, "Sake image")
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
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


def submit_pbs(
    script_path: Path,
    dependency_job_id: str | Sequence[str] | None = None,
) -> str:
    """Submit a PBS script via ``qsub`` and return the job ID."""
    command = ["qsub"]
    if dependency_job_id:
        dependency_job_ids = (
            (dependency_job_id,)
            if isinstance(dependency_job_id, str)
            else tuple(dependency_job_id)
        )
        if any(not job_id for job_id in dependency_job_ids):
            raise PBSError("PBS dependency job IDs must not be empty")
        command.extend(["-W", f"depend=afterok:{':'.join(dependency_job_ids)}"])
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


def submit_pbs_text(
    script: str,
    dependency_job_id: str | Sequence[str] | None = None,
) -> str:
    """Submit a PBS script to ``qsub`` via stdin and return the job ID.

    Stdin submission avoids needing a script path visible to the PBS server,
    which the S3-compatible stages lack (no shared run directory).
    """
    command = ["qsub"]
    if dependency_job_id:
        dependency_job_ids = (
            (dependency_job_id,)
            if isinstance(dependency_job_id, str)
            else tuple(dependency_job_id)
        )
        if any(not job_id for job_id in dependency_job_ids):
            raise PBSError("PBS dependency job IDs must not be empty")
        command.extend(["-W", f"depend=afterok:{':'.join(dependency_job_ids)}"])
    result = subprocess.run(
        command,
        check=False,
        text=True,
        input=script,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise PBSError(f"qsub failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def persist_pbs_job_id(run_dir: Path, stage: str, job_id: str) -> None:
    marker_dir = run_dir / ".pbs"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / f"{stage}.jobid").write_text(f"{job_id}\n", encoding="utf-8")


def _read_pbs_job_id(run_dir: Path, stage: str) -> str | None:
    jobid_path = run_dir / ".pbs" / f"{stage}.jobid"
    if not jobid_path.is_file():
        return None
    job_id = jobid_path.read_text(encoding="utf-8").strip()
    return job_id or None


def _qstat_job_state(job_id: str) -> str | None:
    if shutil.which("qstat") is None:
        return None
    result = subprocess.run(
        ["qstat", "-x", "-f", job_id],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        # Only an explicit unknown-job answer means the job is gone. Any other
        # failure (PBS server restart, network hiccup) is inconclusive and must
        # not be treated as job death.
        stderr = result.stderr.lower()
        if "unknown job" in stderr or "job has finished" in stderr:
            return "MISSING"
        # Some OpenPBS installations disable job history, making ``qstat -x``
        # unusable. A plain query still reports active jobs and returns an
        # explicit unknown-job error once a non-historic job has disappeared.
        result = subprocess.run(
            ["qstat", "-f", job_id],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "unknown job" in stderr or "job has finished" in stderr:
                return "MISSING"
            return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("job_state ="):
            return line.split("=", 1)[1].strip()
    return None


def qdel_pbs_job(job_id: str) -> None:
    if shutil.which("qdel") is None:
        print(f"[pbs] qdel unavailable; cannot cancel PBS job {job_id}")
        return
    result = subprocess.run(
        ["qdel", job_id],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(f"[pbs] qdel {job_id} failed (exit {result.returncode}): {result.stderr.strip()}")


def pbs_job_probe(job_id: str) -> Callable[[], str]:
    """Build a qstat-backed liveness probe for ``wait_for_s3_marker``."""

    def probe() -> str:
        state = _qstat_job_state(job_id)
        if state in PBS_TERMINAL_STATES or state == "MISSING":
            return PROBE_TERMINAL
        if state is None:
            return PROBE_UNKNOWN
        if state in PBS_ACTIVE_STATES:
            return PROBE_RUNNING
        raise PBSError(f"PBS job has unexpected qstat state: {job_id} (state {state})")

    return probe


def qdel_pbs_stage(run_dir: Path, stage: str) -> None:
    job_id = _read_pbs_job_id(run_dir, stage)
    if job_id:
        qdel_pbs_job(job_id)


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
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
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


def render_mappings_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
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
    """Render the Wasabi mappings/Sake stage over S3-backed inputs."""
    require_safe_image(enumerator_image, "enumerator image")
    require_safe_image(sake_image, "Sake image")
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    if mode not in {"numeric", "all"}:
        raise PBSError("CoinJoin mappings mode must be numeric or all")
    for value, description in (
        (mining_fee_rate, "mapping mining fee rate"),
        (max_decomposition_fee, "mapping maximum decomposition fee"),
        (sake_seed, "Sake seed"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PBSError(f"{description} must be a non-negative integer")
    for value, description in (
        (timeout, "mapping timeout"),
        (retry_timeout, "mapping retry timeout"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PBSError(f"{description} must be a positive integer")
    if (
        isinstance(coordination_fee_rate, bool)
        or not isinstance(coordination_fee_rate, (int, float))
        or coordination_fee_rate < 0
    ):
        raise PBSError("mapping coordination fee rate must be non-negative")

    values = _s3_values(artifact_uri, run_id, endpoint_url, credentials_file, profile)
    template = (Path(__file__).parent / "mappings_s3_template.sh").read_text(
        encoding="utf-8"
    )
    return template.format(
        **values,
        enumerator_image=shell_assignment("ENUMERATOR_IMAGE", enumerator_image).split("=", 1)[1],
        sake_image=shell_assignment("SAKE_IMAGE", sake_image).split("=", 1)[1],
        enumerator_image_value=enumerator_image,
        sake_image_value=sake_image,
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
        s5cmd_check=render_s5cmd_check(),
        clear_markers="\n".join(
            (
                render_s5cmd_rm('"$ARTIFACT_URI/$RUN_ID/.pbs/coinjoin-mappings.done"') + " || true",
                render_s5cmd_rm('"$ARTIFACT_URI/$RUN_ID/.pbs/coinjoin-mappings.failed"') + " || true",
            )
        ),
        download_input=render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/*"',
            '"$RUN_WORK/coinjoin-analysis_data/"',
        ),
        upload_outputs=render_s5cmd_sync(
            '"$OUT/"', '"$ARTIFACT_URI/$RUN_ID/coinjoin-mappings_data/"'
        ),
        upload_failed=render_s5cmd_cp(
            '"$FAILED_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/coinjoin-mappings.failed"'
        ),
        upload_done=render_s5cmd_cp(
            '"$DONE_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/coinjoin-mappings.done"'
        ),
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
    include_report: bool = True,
    export_analysis: bool = False,
) -> str:
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
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
        coinjoin_analysis_check=(
            'test -f "$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json" || {\n'
            '  echo "BlockSci S3-compatible reporting requires '
            'coinjoin-analysis_data/coinjoin_tx_info.json" >&2\n'
            "  exit 1\n"
            "}"
            if include_report
            else ""
        ),
        report_output_check=(
            'REPORT_DIR="$RUN_WORK/coinjoinPipeline_data"\n'
            'test -f "$REPORT_DIR/unified_report.json" || {\n'
            '  echo "BlockSci S3-compatible reporting did not produce '
            'coinjoinPipeline_data/unified_report.json" >&2\n'
            "  exit 1\n"
            "}"
            if include_report
            else ""
        ),
        analysis_output_check=(
            'test -f "$RUN_WORK/blocksci-analysis_data/blocksci_analysis.json" || {\n'
            '  echo "BlockSci analysis did not produce '
            'blocksci-analysis_data/blocksci_analysis.json" >&2\n'
            "  exit 1\n"
            "}"
            if export_analysis
            else ""
        ),
        upload_blocksci=render_s5cmd_sync('"$RUN_WORK/blocksci_data/"', '"$ARTIFACT_URI/$RUN_ID/blocksci_data/"'),
        upload_analysis=(
            render_s5cmd_sync(
                '"$RUN_WORK/blocksci-analysis_data/"',
                '"$ARTIFACT_URI/$RUN_ID/blocksci-analysis_data/"',
            )
            if export_analysis
            else ""
        ),
        upload_report=(
            render_s5cmd_sync(
                '"$REPORT_DIR/"',
                '"$ARTIFACT_URI/$RUN_ID/coinjoinPipeline_data/"',
            )
            if include_report
            else ""
        ),
        upload_logs=render_s5cmd_sync('"$RUN_WORK/logs/"', '"$ARTIFACT_URI/$RUN_ID/logs/"'),
        upload_failed=render_s5cmd_cp('"$FAILED_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci.failed"'),
        upload_done=render_s5cmd_cp('"$DONE_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci.done"'),
    )


def render_blocksci_parse_s3_pbs(
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
    external_bitcoin_datadir: Path | None = None,
    external_blocksci_dir: Path | None = None,
    external_network: str | None = None,
    external_max_block: int | None = None,
) -> str:
    """Render a parser-only job that publishes a checksummed reusable index."""
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    if external_bitcoin_datadir is not None and external_blocksci_dir is not None:
        raise PBSError("Choose either an external Bitcoin datadir or an external BlockSci index, not both")

    download_inputs = ""
    source_kind = "emulator"
    network = "bitcoin_regtest"
    source_description = "emulator Bitcoin and exported-block inputs"
    if external_bitcoin_datadir is not None:
        bitcoin_path = external_bitcoin_datadir.expanduser().resolve()
        require_storage_path(bitcoin_path)
        require_existing_path(bitcoin_path, "external Bitcoin coin directory")
        if not (bitcoin_path / "blocks").is_dir():
            raise PBSError(
                "External Bitcoin coin directory must contain blocks/: "
                f"{bitcoin_path}"
            )
        if external_network not in {"bitcoin", "bitcoin_testnet", "bitcoin_regtest"}:
            raise PBSError("External BlockSci network must be bitcoin, bitcoin_testnet, or bitcoin_regtest")
        if (
            isinstance(external_max_block, bool)
            or not isinstance(external_max_block, int)
            or external_max_block < 0
        ):
            raise PBSError("External Bitcoin parsing requires a non-negative --blocksci-max-block")
        source_kind = "external-bitcoin"
        network = external_network
        source_description = "external Bitcoin Core block directory"
        prepare_source = (
            f"BITCOIN_DATADIR={shell_assignment('BITCOIN_DATADIR', str(bitcoin_path)).split('=', 1)[1]}\n"
            f"EXPORTED_MAX_BLOCK={external_max_block}\n"
            'test -d "$BITCOIN_DATADIR/blocks"'
        )
        produce_index = (
            'echo "[blocksci-parse] parsing external chain through block $EXPORTED_MAX_BLOCK"\n'
            'singularity exec \\\n'
            '  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \\\n'
            '  --bind "$BITCOIN_DATADIR:/mnt/data:ro" \\\n'
            '  --env PBS_RUN_ID="$RUN_ID" --env PBS_EXPORTED_MAX_BLOCK="$EXPORTED_MAX_BLOCK" "$IMAGE" \\\n'
            f"  bash -c 'cd \"/runs/emulation/logs/$PBS_RUN_ID\" && {command}'"
        )
    elif external_blocksci_dir is not None:
        blocksci_path = external_blocksci_dir.expanduser().resolve()
        require_storage_path(blocksci_path)
        require_existing_path(blocksci_path, "external BlockSci directory")
        if not (blocksci_path / "config.json").is_file():
            raise PBSError(f"External BlockSci directory must contain config.json: {blocksci_path}")
        if not (blocksci_path / "parsed" / "chain" / "block.dat").is_file():
            raise PBSError(
                "External BlockSci directory must contain parsed/chain/block.dat: "
                f"{blocksci_path}"
            )
        source_kind = "external-blocksci"
        network = "from-config"
        source_description = "existing external BlockSci index"
        prepare_source = (
            f"EXTERNAL_BLOCKSCI_DIR={shell_assignment('EXTERNAL_BLOCKSCI_DIR', str(blocksci_path)).split('=', 1)[1]}\n"
            'test -f "$EXTERNAL_BLOCKSCI_DIR/config.json"\n'
            'test -f "$EXTERNAL_BLOCKSCI_DIR/parsed/chain/block.dat"'
        )
        produce_index = (
            'cp -a "$EXTERNAL_BLOCKSCI_DIR" "$RUN_WORK/blocksci_data"\n'
            'CANONICAL_PARSED="/runs/emulation/logs/$RUN_ID/blocksci_data/parsed"\n'
            "sed -i -E 's#(\"dataDirectory\"[[:space:]]*:[[:space:]]*)\"[^\"]*\"#\\1\"'"
            '"$CANONICAL_PARSED"'"'\"#' \"$RUN_WORK/blocksci_data/config.json\"\n"
            'grep -Fq "$CANONICAL_PARSED" "$RUN_WORK/blocksci_data/config.json" || { '
            'echo "Could not canonicalize external BlockSci dataDirectory" >&2; exit 1; }\n'
            "MAX_BLOCK_NUM=\"$(sed -nE 's/.*\"maxBlockNum\"[[:space:]]*:[[:space:]]*([0-9]+).*/\\1/p' "
            '"$RUN_WORK/blocksci_data/config.json" | head -n 1)"\n'
            'test -n "$MAX_BLOCK_NUM" && [ "$MAX_BLOCK_NUM" -gt 0 ] || { '
            'echo "External BlockSci config has no positive parser.maxBlockNum" >&2; exit 1; }\n'
            'EXPORTED_MAX_BLOCK="$((MAX_BLOCK_NUM - 1))"\n'
            'echo "[blocksci-parse] imported external index through block $EXPORTED_MAX_BLOCK"'
        )
    else:
        download_inputs = "\n".join(
            (
                render_s5cmd_sync(
                    '"$ARTIFACT_URI/$RUN_ID/bitcoin_data/*"',
                    '"$RUN_WORK/bitcoin_data/"',
                ),
                render_s5cmd_sync(
                    '"$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/data/btc-node/*"',
                    '"$RUN_WORK/coinjoin_emulator_data/data/btc-node/"',
                ),
            )
        )
        prepare_source = (
            f"{download_inputs}\n"
            'BITCOIN_DATADIR="$RUN_WORK/bitcoin_data"\n'
            'if [ ! -d "$BITCOIN_DATADIR/regtest/blocks" ] && '
            '[ -d "$BITCOIN_DATADIR/data/regtest/blocks" ]; then\n'
            '  BITCOIN_DATADIR="$BITCOIN_DATADIR/data"\n'
            'fi\n'
            'test -d "$BITCOIN_DATADIR/regtest/blocks" || {\n'
            '  echo "BlockSci parsing requires a Bitcoin datadir containing regtest/blocks" >&2\n'
            '  exit 1\n'
            '}\n'
            'EXPORTED_MAX_BLOCK="$(find "$RUN_WORK/coinjoin_emulator_data/data/btc-node" '
            "-maxdepth 1 -type f -name 'block_*.json' -printf '%f\\n' | "
            "sed -nE 's/^block_([0-9]+)\\.json$/\\1/p' | sort -n | tail -n 1)" + '"\n'
            'test -n "$EXPORTED_MAX_BLOCK" || { '
            'echo "BlockSci parsing could not determine the exported maximum block" >&2; exit 1; }'
        )
        produce_index = (
            'echo "[blocksci-parse] parsing through exported block $EXPORTED_MAX_BLOCK"\n'
            'singularity exec \\\n'
            '  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \\\n'
            '  --bind "$BITCOIN_DATADIR:/mnt/data:ro" \\\n'
            '  --env PBS_RUN_ID="$RUN_ID" --env PBS_EXPORTED_MAX_BLOCK="$EXPORTED_MAX_BLOCK" "$IMAGE" \\\n'
            f"  bash -c 'cd \"/runs/emulation/logs/$PBS_RUN_ID\" && {command}'"
        )
    values = _s3_values(artifact_uri, run_id, endpoint_url, credentials_file, profile)
    template = (Path(__file__).parent / "blocksci_parse_s3_template.sh").read_text(
        encoding="utf-8"
    )
    return template.format(
        **values,
        image=shell_assignment("IMAGE", image).split("=", 1)[1],
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        s5cmd_check=render_s5cmd_check(),
        clear_markers="\n".join(
            (
                render_s5cmd_rm('"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci-parse.done"') + " || true",
                render_s5cmd_rm('"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci-parse.failed"') + " || true",
            )
        ),
        source_description=source_description,
        source_kind=source_kind,
        network=network,
        prepare_source=prepare_source,
        produce_index=produce_index,
        upload_cache=render_s5cmd_sync(
            '"$CACHE_DIR/"', '"$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/"'
        ),
        upload_failed=render_s5cmd_cp(
            '"$FAILED_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci-parse.failed"'
        ),
        upload_done=render_s5cmd_cp(
            '"$DONE_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci-parse.done"'
        ),
    )


def render_blocksci_update_s3_pbs(
    artifact_uri: str,
    run_id: str,
    source_run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    external_bitcoin_datadir: Path,
    external_network: str,
    external_max_block: int,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
) -> str:
    """Render a job that incrementally updates one S3 cache into a fresh run."""
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    try:
        source_run_id = validate_run_id(source_run_id)
    except ValueError as error:
        raise PBSError(str(error)) from error
    if source_run_id == run_id:
        raise PBSError("Incremental BlockSci update requires different source and target run IDs")
    bitcoin_path = external_bitcoin_datadir.expanduser().resolve()
    require_storage_path(bitcoin_path)
    require_existing_path(bitcoin_path, "external Bitcoin coin directory")
    if not (bitcoin_path / "blocks").is_dir():
        raise PBSError(
            "External Bitcoin coin directory must contain blocks/: "
            f"{bitcoin_path}"
        )
    if external_network not in {"bitcoin", "bitcoin_testnet", "bitcoin_regtest"}:
        raise PBSError("External BlockSci network must be bitcoin, bitcoin_testnet, or bitcoin_regtest")
    if (
        isinstance(external_max_block, bool)
        or not isinstance(external_max_block, int)
        or external_max_block < 0
    ):
        raise PBSError("External Bitcoin parsing requires a non-negative --blocksci-max-block")

    values = _s3_values(artifact_uri, run_id, endpoint_url, credentials_file, profile)
    template = (Path(__file__).parent / "blocksci_update_s3_template.sh").read_text(
        encoding="utf-8"
    )
    return template.format(
        **values,
        source_run_id=shell_assignment("SOURCE_RUN_ID", source_run_id).split("=", 1)[1],
        image=shell_assignment("IMAGE", image).split("=", 1)[1],
        network=shell_assignment("NETWORK", external_network).split("=", 1)[1],
        exported_max_block=external_max_block,
        bitcoin_datadir=shell_assignment("BITCOIN_DATADIR", str(bitcoin_path)).split("=", 1)[1],
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        s5cmd_check=render_s5cmd_check(),
        clear_markers="\n".join(
            (
                render_s5cmd_rm('"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci-update.done"') + " || true",
                render_s5cmd_rm('"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci-update.failed"') + " || true",
            )
        ),
        download_source_cache=render_s5cmd_sync(
            '"$ARTIFACT_URI/$SOURCE_RUN_ID/blocksci-parse_data/*"',
            '"$SOURCE_CACHE_DIR/"',
        ),
        upload_cache=render_s5cmd_sync(
            '"$CACHE_DIR/"', '"$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/"'
        ),
        upload_failed=render_s5cmd_cp(
            '"$FAILED_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci-update.failed"'
        ),
        upload_done=render_s5cmd_cp(
            '"$DONE_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/blocksci-update.done"'
        ),
    )


def render_blocksci_analyze_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    mode: str = "blocksci-analyze",
    user_script: Path | None = None,
    notebooks_dir: Path | None = None,
    notebook_port: int = 8888,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
) -> str:
    """Render analysis, custom-script, or notebook work over a cached index."""
    if mode not in {"blocksci-analyze", "blocksci-script", "blocksci-notebook"}:
        raise PBSError(f"Unsupported reusable BlockSci mode: {mode}")
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    require_safe_pbs_token(mode, "PBS stage")
    if isinstance(notebook_port, bool) or not isinstance(notebook_port, int) or not (1024 <= notebook_port <= 65535):
        raise PBSError("BlockSci notebook port must be between 1024 and 65535")

    extra_binds = ""
    prepare_mode = ""
    connection_help = ""
    output_check = ""
    upload_sources: list[tuple[str, str]] = []
    if mode == "blocksci-analyze":
        prepare_mode = 'mkdir -p "$RUN_WORK/blocksci-analysis_data"'
        output_check = (
            'test -f "$RUN_WORK/blocksci-analysis_data/blocksci_analysis.json" || {\n'
            '  echo "Reusable BlockSci analysis did not produce blocksci-analysis_data/blocksci_analysis.json" >&2\n'
            "  exit 1\n"
            "}"
        )
        upload_sources.append(
            (
                '"$RUN_WORK/blocksci-analysis_data/"',
                '"$ARTIFACT_URI/$RUN_ID/blocksci-analysis_data/"',
            )
        )
    elif mode == "blocksci-script":
        if user_script is None:
            raise PBSError("Reusable BlockSci script mode requires --blocksci-script")
        script_path = user_script.expanduser().resolve()
        require_storage_path(script_path)
        require_existing_path(script_path, "BlockSci user script")
        if not script_path.is_file():
            raise PBSError(f"BlockSci user script is not a file: {script_path}")
        prepare_mode = (
            'mkdir -p "$RUN_WORK/blocksci-custom-analysis_data"\n'
            f"USER_SCRIPT={shell_assignment('USER_SCRIPT', str(script_path)).split('=', 1)[1]}\n"
            'cp "$USER_SCRIPT" "$RUN_WORK/blocksci-custom-analysis_data/script.py"\n'
            'sha256sum "$USER_SCRIPT" > "$RUN_WORK/blocksci-custom-analysis_data/script.py.sha256"'
        )
        extra_binds = 'EXTRA_BINDS+=(--bind "$USER_SCRIPT:/mnt/user-analysis.py:ro")'
        upload_sources.append(
            (
                '"$RUN_WORK/blocksci-custom-analysis_data/"',
                '"$ARTIFACT_URI/$RUN_ID/blocksci-custom-analysis_data/"',
            )
        )
    else:
        if notebooks_dir is not None:
            notebook_path = notebooks_dir.expanduser().resolve()
            require_storage_path(notebook_path)
            require_existing_path(notebook_path, "BlockSci notebooks directory")
            if not notebook_path.is_dir():
                raise PBSError(f"BlockSci notebooks path is not a directory: {notebook_path}")
            prepare_mode = (
                f"NOTEBOOK_DIR={shell_assignment('NOTEBOOK_DIR', str(notebook_path)).split('=', 1)[1]}"
            )
        else:
            prepare_mode = (
                'NOTEBOOK_DIR="$RUN_WORK/blocksci-notebooks_data"\n'
                'mkdir -p "$NOTEBOOK_DIR"'
            )
        extra_binds = 'EXTRA_BINDS+=(--bind "$NOTEBOOK_DIR:/mnt/notebooks:rw")'
        connection_help = (
            f'echo "[blocksci-notebook] Jupyter port: {notebook_port}"\n'
            'LOGIN="${PBS_O_LOGNAME:-${USER:-<login>}}"\n'
            'FRONTEND="${PBS_O_HOST:-<frontend>}"\n'
            f'echo "[blocksci-notebook] Tunnel: ssh -N -J $LOGIN@$FRONTEND '
            f'-L {notebook_port}:127.0.0.1:{notebook_port} $LOGIN@$(hostname -f)"\n'
            'echo "[blocksci-notebook] The Jupyter token follows below."'
        )
        upload_sources.append(
            ('"$NOTEBOOK_DIR/"', '"$ARTIFACT_URI/$RUN_ID/blocksci-notebooks_data/"')
        )

    upload_outputs = "\n  ".join(
        f"{render_s5cmd_sync(source, destination)} || upload_status=$?"
        for source, destination in upload_sources
    )
    downloads = [
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/*"',
            '"$CACHE_DIR/"',
        )
    ]
    if mode == "blocksci-analyze":
        downloads.extend(
            (
                render_s5cmd_sync(
                    '"$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*"',
                    '"$RUN_WORK/.pipeline/exporters/"',
                ),
                render_s5cmd_sync(
                    '"$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/*"',
                    '"$RUN_WORK/coinjoin_emulator_data/"',
                ),
            )
        )
    values = _s3_values(artifact_uri, run_id, endpoint_url, credentials_file, profile)
    template = (Path(__file__).parent / "blocksci_analyze_s3_template.sh").read_text(
        encoding="utf-8"
    )
    return template.format(
        **values,
        image=shell_assignment("IMAGE", image).split("=", 1)[1],
        command=command,
        mode=shell_assignment("MODE", mode).split("=", 1)[1],
        stage=mode,
        job_name=mode.replace("-", "_") + "_s3",
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        s5cmd_check=render_s5cmd_check(),
        clear_markers="\n".join(
            (
                render_s5cmd_rm(f'"$ARTIFACT_URI/$RUN_ID/.pbs/{mode}.done"') + " || true",
                render_s5cmd_rm(f'"$ARTIFACT_URI/$RUN_ID/.pbs/{mode}.failed"') + " || true",
            )
        ),
        download_inputs="\n".join(downloads),
        prepare_mode=prepare_mode,
        extra_binds=extra_binds,
        connection_help=connection_help,
        output_check=output_check,
        upload_outputs=upload_outputs,
        upload_failed=render_s5cmd_cp(
            '"$FAILED_MARKER"', f'"$ARTIFACT_URI/$RUN_ID/.pbs/{mode}.failed"'
        ),
        upload_done=render_s5cmd_cp(
            '"$DONE_MARKER"', f'"$ARTIFACT_URI/$RUN_ID/.pbs/{mode}.done"'
        ),
    )


def render_unified_report_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_UNIFIED_REPORT_NCPUS,
    mem: str = DEFAULT_UNIFIED_REPORT_MEM,
    scratch: str = DEFAULT_UNIFIED_REPORT_SCRATCH,
    walltime: str = DEFAULT_UNIFIED_REPORT_WALLTIME,
    include_mappings: bool = False,
) -> str:
    """Render the S3 report-only job that joins both analyzer outputs."""
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    values = _s3_values(artifact_uri, run_id, endpoint_url, credentials_file, profile)
    template = (Path(__file__).parent / "unified_report_s3_template.sh").read_text(
        encoding="utf-8"
    )
    downloads = [
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*"',
            '"$RUN_WORK/.pipeline/exporters/"',
        ),
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/*"',
            '"$RUN_WORK/coinjoin_emulator_data/"',
        ),
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/*"',
            '"$RUN_WORK/coinjoin-analysis_data/"',
        ),
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/blocksci-analysis_data/*"',
            '"$RUN_WORK/blocksci-analysis_data/"',
        ),
    ]
    if include_mappings:
        downloads.append(
            render_s5cmd_sync(
                '"$ARTIFACT_URI/$RUN_ID/coinjoin-mappings_data/*"',
                '"$RUN_WORK/coinjoin-mappings_data/"',
            )
        )
    return template.format(
        **values,
        image=shell_assignment("IMAGE", image).split("=", 1)[1],
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        s5cmd_check=render_s5cmd_check(),
        download_inputs="\n".join(downloads),
        upload_report=render_s5cmd_sync(
            '"$REPORT_DIR/"',
            '"$ARTIFACT_URI/$RUN_ID/coinjoinPipeline_data/"',
        ),
        upload_failed=render_s5cmd_cp(
            '"$FAILED_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/unified-report.failed"'
        ),
        upload_done=render_s5cmd_cp(
            '"$DONE_MARKER"', '"$ARTIFACT_URI/$RUN_ID/.pbs/unified-report.done"'
        ),
    )


def _submit_s3_script(
    script: str,
    stage: str,
    dry_run: bool,
    dependency_job_id: str | Sequence[str] | None = None,
) -> str | None:
    if dry_run:
        print(f"[dry-run] PBS S3-compatible script for {stage}:\n{script}")
        return None
    require_qsub()
    job_id = submit_pbs_text(script, dependency_job_id)
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


def submit_mappings_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
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
    dependency_job_id: str | None = None,
) -> str | None:
    script = render_mappings_s3_pbs(
        artifact_uri,
        run_id,
        endpoint_url,
        credentials_file,
        profile,
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
    return _submit_s3_script(
        script, "coinjoin-mappings", dry_run, dependency_job_id
    )


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
    include_report: bool = True,
    export_analysis: bool = False,
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
        include_report=include_report,
        export_analysis=export_analysis,
    )
    return _submit_s3_script(script, "blocksci", dry_run, dependency_job_id)


def submit_blocksci_parse_s3_pbs(
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
    external_bitcoin_datadir: Path | None = None,
    external_blocksci_dir: Path | None = None,
    external_network: str | None = None,
    external_max_block: int | None = None,
    dry_run: bool = False,
) -> str | None:
    script = render_blocksci_parse_s3_pbs(
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
        external_bitcoin_datadir=external_bitcoin_datadir,
        external_blocksci_dir=external_blocksci_dir,
        external_network=external_network,
        external_max_block=external_max_block,
    )
    return _submit_s3_script(script, "blocksci-parse", dry_run)


def submit_blocksci_update_s3_pbs(
    artifact_uri: str,
    run_id: str,
    source_run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    external_bitcoin_datadir: Path,
    external_network: str,
    external_max_block: int,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
    dry_run: bool = False,
) -> str | None:
    script = render_blocksci_update_s3_pbs(
        artifact_uri,
        run_id,
        source_run_id,
        endpoint_url,
        credentials_file,
        profile,
        image,
        command,
        external_bitcoin_datadir=external_bitcoin_datadir,
        external_network=external_network,
        external_max_block=external_max_block,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )
    return _submit_s3_script(script, "blocksci-update", dry_run)


def submit_blocksci_analyze_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    mode: str = "blocksci-analyze",
    user_script: Path | None = None,
    notebooks_dir: Path | None = None,
    notebook_port: int = 8888,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
    dry_run: bool = False,
    dependency_job_id: str | None = None,
) -> str | None:
    script = render_blocksci_analyze_s3_pbs(
        artifact_uri,
        run_id,
        endpoint_url,
        credentials_file,
        profile,
        image,
        command,
        mode=mode,
        user_script=user_script,
        notebooks_dir=notebooks_dir,
        notebook_port=notebook_port,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )
    return _submit_s3_script(script, mode, dry_run, dependency_job_id)


def submit_unified_report_s3_pbs(
    artifact_uri: str,
    run_id: str,
    endpoint_url: str,
    credentials_file: str,
    profile: str,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_UNIFIED_REPORT_NCPUS,
    mem: str = DEFAULT_UNIFIED_REPORT_MEM,
    scratch: str = DEFAULT_UNIFIED_REPORT_SCRATCH,
    walltime: str = DEFAULT_UNIFIED_REPORT_WALLTIME,
    dry_run: bool = False,
    dependency_job_ids: Sequence[str] = (),
    include_mappings: bool = False,
) -> str | None:
    script = render_unified_report_s3_pbs(
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
        include_mappings=include_mappings,
    )
    return _submit_s3_script(
        script,
        "unified-report",
        dry_run,
        dependency_job_ids,
    )


def wait_for_pbs_marker(
    run_dir: Path,
    stage: str,
    poll_interval: int = POLL_INTERVAL_SECONDS,
    *,
    job_id: str | None = None,
    timeout_seconds: int | None = None,
) -> None:
    """Block until the PBS stage writes a marker, with qstat and deadline fallbacks."""
    done = run_dir / ".pbs" / f"{stage}.done"
    failed = run_dir / ".pbs" / f"{stage}.failed"
    job_id = job_id or _read_pbs_job_id(run_dir, stage)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    terminal_state_seen: str | None = None

    while True:
        if failed.exists():
            raise PBSError(f"PBS stage failed: {stage}")
        if done.exists():
            return
        # Not polled during the grace cycle: the terminal state already landed.
        state = _qstat_job_state(job_id) if job_id and terminal_state_seen is None else None
        if deadline is not None and time.monotonic() >= deadline:
            if state not in PBS_ACTIVE_STATES:
                raise PBSError(f"Timed out waiting for PBS stage marker: {stage}")
            # The job is verifiably alive (queued or running); shared-cluster
            # queue time must not be counted against the walltime budget.
            deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
            print(
                f"[WARN] {stage} exceeded its wait budget but job {job_id} is still "
                f"in state {state}; extending the deadline.",
                file=sys.stderr,
            )
        if terminal_state_seen is not None:
            # The compute node writes the marker over shared storage, which can
            # lag behind qstat; one extra poll cycle already passed without it.
            raise PBSError(
                f"PBS stage ended without marker: {stage} (job {job_id}, state {terminal_state_seen})"
            )
        if job_id:
            if state in PBS_TERMINAL_STATES or state == "MISSING":
                terminal_state_seen = state
            elif state is not None and state not in PBS_ACTIVE_STATES:
                raise PBSError(f"PBS stage has unexpected qstat state: {stage} (job {job_id}, state {state})")
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
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
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
    persist_pbs_job_id(run_dir, stage, job_id)
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
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
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
    persist_pbs_job_id(run_dir, "coinjoin-analysis", job_id)
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
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
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
    persist_pbs_job_id(run_dir, "coinjoin-mappings", job_id)
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
            "BLOCKSCI_RUN_DIR={run_dir_container} python3 {blocksci_script}"
        )
    if export_analysis:
        parts.append(
            "python3 /mnt/exporters/blocksci_analysis.py "
            "--config {config} "
            "--run-dir {run_dir_container} "
            "--coinjoin-type {coinjoin_type} "
            "--min-input-count {min_input_count} "
            "--joinmarket-detector {joinmarket_detector} "
            "--joinmarket-min-base-fee {joinmarket_min_base_fee} "
            "--joinmarket-percentage-fee {joinmarket_percentage_fee} "
            "--joinmarket-max-depth {joinmarket_max_depth}"
        )
        if test_values:
            parts[-1] += " --test-values"
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
    test_values: bool,
) -> str:
    """Build detector analysis over an already parsed BlockSci index."""
    config = f"/runs/emulation/logs/{run_id}/blocksci_data/config.json"
    run_dir = f"/runs/emulation/logs/{run_id}"
    command = (
        "python3 /mnt/exporters/blocksci_analysis.py "
        f"--config {config} --run-dir {run_dir} "
        f"--coinjoin-type {coinjoin_type} "
        f"--min-input-count {min_input_count if min_input_count is not None else 'default'} "
        f"--joinmarket-detector {joinmarket_detector} "
        f"--joinmarket-min-base-fee {joinmarket_min_base_fee} "
        f"--joinmarket-percentage-fee {joinmarket_percentage_fee} "
        f"--joinmarket-max-depth {joinmarket_max_depth}"
    )
    if test_values:
        command += " --test-values"
    return command


def blocksci_script_pbs_command(run_id: str) -> str:
    """Build custom-script execution over an already parsed BlockSci index."""
    run_dir = f"/runs/emulation/logs/{run_id}"
    config = f"{run_dir}/blocksci_data/config.json"
    output = f"{run_dir}/blocksci-custom-analysis_data"
    return (
        f"ACTIVE_RUN_ID={run_id} BLOCKSCI_CONFIG={config} "
        f"BLOCKSCI_RUN_DIR={run_dir} BLOCKSCI_OUTPUT_DIR={output} "
        "python3 /mnt/user-analysis.py"
    )


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
    test_values: bool,
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
    if test_values:
        command += " --test-values"
    return command


def coinjoin_analysis_pbs_command(action: str = "collect_docker") -> str:
    """Build the in-container command for the coinjoin-analysis PBS stage."""
    return f"python -m cj_process.parse_cj_logs --action {action} --target-path /runs/emulation/selected"
