"""PBS submission, polling, marker persistence, and local stage submission."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from client.artifacts import (
    PROBE_QUEUED,
    PROBE_RUNNING,
    PROBE_TERMINAL,
    PROBE_UNKNOWN,
    S3Target,
)

from .defaults import (
    DEFAULT_BLOCKSCI_MEM,
    DEFAULT_BLOCKSCI_NCPUS,
    DEFAULT_BLOCKSCI_SCRATCH,
    DEFAULT_BLOCKSCI_WALLTIME,
    DEFAULT_COINJOIN_ANALYSIS_MEM,
    DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
    DEFAULT_UNIFIED_REPORT_MEM,
    DEFAULT_UNIFIED_REPORT_NCPUS,
    DEFAULT_UNIFIED_REPORT_SCRATCH,
    DEFAULT_UNIFIED_REPORT_WALLTIME,
    PBS_ACTIVE_STATES,
    PBS_QUEUED_STATES,
    PBS_TERMINAL_STATES,
    POLL_INTERVAL_SECONDS,
    STAGE_LOG_SETTLE_SECONDS,
    STAGE_LOG_TAIL_LINES,
)
from .templates_local import (
    render_blocksci_pbs,
    render_coinjoin_analysis_pbs,
    render_mappings_pbs,
)
from .templates_s3 import (
    render_blocksci_analyze_s3_pbs,
    render_blocksci_parse_s3_pbs,
    render_blocksci_s3_pbs,
    render_blocksci_update_s3_pbs,
    render_coinjoin_analysis_s3_pbs,
    render_mappings_s3_pbs,
    render_unified_report_s3_pbs,
)
from .validation import (
    PBSError,
    require_bitcoin_datadir,
    require_existing_path,
    require_qsub,
    require_safe_pbs_token,
    require_storage_path,
)


def _parse_qsub_job_id(stdout: str) -> str:
    """Validate the job ID qsub printed on a zero exit.

    A zero exit with unusable stdout is worse than a failure: the scheduler may
    well have accepted the job, but an unrecorded job ID cannot be persisted,
    depended on, or cancelled during rollback. Fail loudly instead.
    """
    job_id = (stdout or "").strip()
    if not job_id or "\n" in job_id:
        raise PBSError(f"qsub returned an invalid job ID: {job_id!r}")
    # MetaCentrum job IDs are `<seq>.<server-fqdn>`, which SAFE_PBS_TOKEN_RE
    # covers. Job arrays (`123[].server`) would be rejected; no stage submits one.
    require_safe_pbs_token(job_id, "PBS job ID")
    return job_id


def qsub_command(
    dependency_job_id: str | Sequence[str] | None = None,
) -> list[str]:
    """Build qsub's common dependency prefix for file and stdin submissions."""
    command = ["qsub"]
    if not dependency_job_id:
        return command
    dependency_job_ids = (
        (dependency_job_id,)
        if isinstance(dependency_job_id, str)
        else tuple(dependency_job_id)
    )
    if any(not job_id for job_id in dependency_job_ids):
        raise PBSError("PBS dependency job IDs must not be empty")
    command.extend(["-W", f"depend=afterok:{':'.join(dependency_job_ids)}"])
    return command


def submit_pbs(
    script_path: Path,
    dependency_job_id: str | Sequence[str] | None = None,
) -> str:
    """Submit a PBS script via ``qsub`` and return the job ID."""
    command = qsub_command(dependency_job_id)
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
    return _parse_qsub_job_id(result.stdout)


def submit_pbs_text(
    script: str,
    dependency_job_id: str | Sequence[str] | None = None,
) -> str:
    """Submit a PBS script to ``qsub`` via stdin and return the job ID.

    Stdin submission avoids needing a script path visible to the PBS server,
    which the S3-compatible stages lack (no shared run directory).
    """
    command = qsub_command(dependency_job_id)
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
    return _parse_qsub_job_id(result.stdout)


def persist_pbs_job_id(run_dir: Path, stage: str, job_id: str) -> None:
    """Record a submitted job ID atomically.

    A truncating write can be interrupted, and the overlap check skips empty
    .jobid files -- which would let a duplicate graph be submitted while the
    recorded job is still active. Write-then-rename never exposes a partial file.
    """
    marker_dir = run_dir / ".pbs"
    marker_dir.mkdir(parents=True, exist_ok=True)
    target = marker_dir / f"{stage}.jobid"
    temp_path = marker_dir / f".{stage}.jobid.tmp"
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(f"{job_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


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


def qdel_pbs_job(job_id: str) -> bool:
    """Cancel a PBS job; return whether the cancellation was confirmed.

    Callers such as ``rollback_s3_pbs_submissions`` must be able to tell a
    cancelled job from one that is still burning allocation, so failures are
    reported rather than only printed.
    """
    if shutil.which("qdel") is None:
        print(f"[pbs] qdel unavailable; cannot cancel PBS job {job_id}", file=sys.stderr)
        return False
    result = subprocess.run(
        ["qdel", job_id],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # A job that already left the queue counts as cancelled for rollback.
        if "unknown job" in stderr.lower() or "job has finished" in stderr.lower():
            return True
        print(
            f"[pbs] qdel {job_id} failed (exit {result.returncode}): {stderr}",
            file=sys.stderr,
        )
        return False
    return True


def pbs_job_probe(job_id: str) -> Callable[[], str]:
    """Build a qstat-backed liveness probe for ``wait_for_s3_marker``."""

    def probe() -> str:
        state = _qstat_job_state(job_id)
        if state in PBS_TERMINAL_STATES or state == "MISSING":
            return PROBE_TERMINAL
        if state is None:
            return PROBE_UNKNOWN
        if state in PBS_QUEUED_STATES:
            return PROBE_QUEUED
        if state in PBS_ACTIVE_STATES:
            return PROBE_RUNNING
        raise PBSError(f"PBS job has unexpected qstat state: {job_id} (state {state})")

    return probe


def qdel_pbs_stage(run_dir: Path, stage: str) -> bool:
    job_id = _read_pbs_job_id(run_dir, stage)
    if job_id:
        return qdel_pbs_job(job_id)
    return True


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
    target: S3Target,
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
        target,
        image,
        command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )
    return _submit_s3_script(script, "coinjoin-analysis", dry_run)


def submit_mappings_s3_pbs(
    target: S3Target,
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
        target,
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
    target: S3Target,
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
        target,
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
    target: S3Target,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
    external_bitcoin_datadir: Path | None = None,
    bitcoin_blocks_uri: str | None = None,
    external_blocksci_dir: Path | None = None,
    external_network: str | None = None,
    external_max_block: int | None = None,
    dry_run: bool = False,
) -> str | None:
    script = render_blocksci_parse_s3_pbs(
        target,
        image,
        command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        external_bitcoin_datadir=external_bitcoin_datadir,
        bitcoin_blocks_uri=bitcoin_blocks_uri,
        external_blocksci_dir=external_blocksci_dir,
        external_network=external_network,
        external_max_block=external_max_block,
    )
    return _submit_s3_script(script, "blocksci-parse", dry_run)


def submit_blocksci_update_s3_pbs(
    target: S3Target,
    source_run_id: str,
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
        target,
        source_run_id,
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
    target: S3Target,
    image: str,
    command: str,
    *,
    mode: str = "blocksci-analyze",
    user_script: Path | None = None,
    external_baseline_uri: str | None = None,
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
        target,
        image,
        command,
        mode=mode,
        user_script=user_script,
        external_baseline_uri=external_baseline_uri,
        notebooks_dir=notebooks_dir,
        notebook_port=notebook_port,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )
    return _submit_s3_script(script, mode, dry_run, dependency_job_id)


def submit_unified_report_s3_pbs(
    target: S3Target,
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
        target,
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


def stage_log_path(run_dir: Path, stage: str) -> Path:
    """Path the shared-storage PBS templates point ``#PBS -o`` at."""
    return run_dir / "logs" / f"{stage}.pbs.log"


def report_stage_log(
    run_dir: Path,
    stage: str,
    *,
    tail_lines: int = STAGE_LOG_TAIL_LINES,
    settle_seconds: int = STAGE_LOG_SETTLE_SECONDS,
) -> Path | None:
    """Print the tail of a finished stage's job log and return its path.

    A marker only says *that* a stage ended; the reason lives in the job's own
    output, which nothing else reads -- so a failed PBS stage used to be
    reported as a bare "PBS stage failed: <stage>" with the evidence left on a
    compute node. PBS copies the spooled log back around the same time the
    marker lands, so wait briefly for it to appear before giving up.
    """
    log_path = stage_log_path(run_dir, stage)
    if not log_path.exists() and log_path.parent.is_dir():
        deadline = time.monotonic() + settle_seconds
        while not log_path.exists() and time.monotonic() < deadline:
            time.sleep(1)
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        print(
            f"[pbs] No {stage} job log available at {log_path} ({error}); "
            "the compute node may not have copied it back.",
            file=sys.stderr,
        )
        return None
    shown = lines[-tail_lines:]
    skipped = len(lines) - len(shown)
    print(f"===== {stage} PBS job log: {log_path} =====", file=sys.stderr)
    if skipped > 0:
        print(f"[... {skipped} earlier lines omitted ...]", file=sys.stderr)
    for line in shown:
        print(line, file=sys.stderr)
    print(f"===== end {stage} PBS job log =====", file=sys.stderr)
    return log_path


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
            report_stage_log(run_dir, stage)
            raise PBSError(
                f"PBS stage failed: {stage} (job log: {stage_log_path(run_dir, stage)})"
            )
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
            report_stage_log(run_dir, stage)
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
