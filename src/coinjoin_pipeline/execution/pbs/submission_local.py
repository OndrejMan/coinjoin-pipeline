"""EXPERIMENTAL: PBS stages that share a run directory with the submitting host.

The Kubernetes → S3 path submits through ``submission.submit_pbs_job`` instead.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from exporters.artifact_paths import BASELINE_FILE

from . import submission
from .defaults import (
    DEFAULT_BLOCKSCI_MEM,
    DEFAULT_BLOCKSCI_NCPUS,
    DEFAULT_BLOCKSCI_SCRATCH,
    DEFAULT_BLOCKSCI_WALLTIME,
    DEFAULT_COINJOIN_ANALYSIS_MEM,
    DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
    PBS_ACTIVE_STATES,
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
from .validation import (
    PBSError,
    require_bitcoin_datadir,
    require_existing_path,
    require_qsub,
    require_storage_path,
)


def _read_pbs_job_id(run_dir: Path, stage: str) -> str | None:
    jobid_path = run_dir / ".pbs" / f"{stage}.jobid"
    if not jobid_path.is_file():
        return None
    job_id = jobid_path.read_text(encoding="utf-8").strip()
    return job_id or None


def qdel_pbs_stage(run_dir: Path, stage: str) -> bool:
    job_id = _read_pbs_job_id(run_dir, stage)
    if job_id:
        return submission.qdel_pbs_job(job_id)
    return True


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
            raise PBSError(f"PBS stage failed: {stage} (job log: {stage_log_path(run_dir, stage)})")
        if done.exists():
            return
        # Not polled during the grace cycle: the terminal state already landed.
        state = submission.qstat_job_state(job_id) if job_id and terminal_state_seen is None else None
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
            raise PBSError(f"PBS stage ended without marker: {stage} (job {job_id}, state {terminal_state_seen})")
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
    if dry_run:
        print(f"[dry-run] PBS script for {stage}:\n{script}")
        return None
    require_qsub()
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    job_id = submission.submit_pbs(script_path)
    submission.persist_pbs_job_id(run_dir, stage, job_id)
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
    if dry_run:
        print(f"[dry-run] PBS script for coinjoin-analysis:\n{script}")
        return None
    require_qsub()
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    job_id = submission.submit_pbs(script_path)
    submission.persist_pbs_job_id(run_dir, "coinjoin-analysis", job_id)
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
    require_existing_path(
        run_dir / BASELINE_FILE,
        "CoinJoin mappings input",
    )
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
    if dry_run:
        print(f"[dry-run] PBS script for coinjoin-mappings:\n{script}")
        return None
    require_qsub()
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    job_id = submission.submit_pbs(script_path)
    submission.persist_pbs_job_id(run_dir, "coinjoin-mappings", job_id)
    print(f"[pbs] Submitted coinjoin-mappings PBS job: {job_id}")
    return job_id
