"""What the pipeline itself recorded about a run, read by ``status``, ``stop`` and ``--resume``.

A run leaves three records outside any process: the PBS job IDs in
``<runs-root>/<run-id>/.pbs/<stage>.jobid``, the Kubernetes resources labelled
``coinjoin.run-id=<run-id>`` and the terminal markers in its S3 prefix. Reading
them here keeps their names in one place instead of in every client.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from .execution.kubernetes import RUN_ID_LABEL, S3_EMULATION_APP_LABEL, kubectl
from .execution.pbs.status import LEFT_SCHEDULER, PBS_COMMAND_TIMEOUT_SECONDS, job_phase, parse_qstat
from .execution.pbs.submission import recorded_pbs_job_ids
from .execution.probes import PROBE_RUNNING, PROBE_TERMINAL
from .execution.pbs.validation import PBSError, require_safe_pbs_token
from .storage.s3 import S3Access, run_prefix_uri, s3_object_exists, stage_marker_uri, upload_marker_uri

QUEUED = "queued"
RUNNING = "running"
FINISHED = "finished"


def recorded_jobs(run_ids: Iterable[str], roots: Iterable[Path]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Recorded PBS job IDs by run and stage; an earlier root wins for the same stage."""
    jobs: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    roots = tuple(roots)
    for run_id in run_ids:
        found: dict[str, str] = {}
        for root in roots:
            run_dir = root / run_id
            for stage, job_id in recorded_pbs_job_ids(run_dir).items():
                if stage in found:
                    continue
                try:
                    require_safe_pbs_token(job_id, "PBS job ID")
                except PBSError:
                    warnings.append(f"Invalid PBS job ID in {run_dir / '.pbs' / stage}.jobid; check that job manually.")
                    continue
                found[stage] = job_id
        jobs[run_id] = found
    return jobs, warnings


def _short(job_id: str) -> str:
    return job_id.split(".", 1)[0]


def scheduler_states(user: str | None = None) -> dict[str, str] | None:
    """Every listed job of the user by short job ID and qstat state, or None when PBS is unavailable.

    One ``qstat -f -u`` answers for any number of runs; a job missing from it has left the queue.
    """
    user = user or os.environ.get("USER") or getpass.getuser()
    try:
        result = subprocess.run(
            ["qstat", "-f", "-u", user],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=PBS_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    states: dict[str, str] = {}
    for block in result.stdout.split("Job Id: ")[1:]:
        job_id, _, body = block.partition("\n")
        state = parse_qstat(body).get("job_state")
        if job_id.strip() and state:
            states[_short(job_id.strip())] = state
    return states


def job_state(job_id: str, states: dict[str, str]) -> str:
    """``queued``, ``running`` or ``finished`` for one job of a ``scheduler_states`` listing.

    A job missing from the listing has left the queue; an undocumented state still holds its place there.
    """
    phase = job_phase(states.get(_short(job_id), LEFT_SCHEDULER))
    return {PROBE_TERMINAL: FINISHED, PROBE_RUNNING: RUNNING}.get(phase or "", QUEUED)


def emulation_job_state(job: dict) -> str:
    """The state of one emulation Job object: done, failed, running or queued."""
    status = job.get("status") or {}
    if status.get("succeeded"):
        return "done"
    if any(
        condition.get("type") == "Failed" and condition.get("status") == "True"
        for condition in status.get("conditions") or []
    ):
        return "failed"
    return RUNNING if status.get("active") else QUEUED


def emulation_states(kubeconfig: Path, namespaces: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    """The emulation Job state of every run in the namespaces, by run ID."""
    states: dict[str, str] = {}
    warnings: list[str] = []
    for namespace in namespaces:
        try:
            result = kubectl(
                kubeconfig,
                "get",
                "jobs",
                "-l",
                f"app.kubernetes.io/name={S3_EMULATION_APP_LABEL}",
                "-o",
                "json",
                "--request-timeout=10s",
                namespace=namespace,
            )
        except FileNotFoundError:
            return states, ["kubectl is missing; Kubernetes status is unavailable."]
        if result.returncode != 0:
            warnings.append(f"Kubernetes status is unavailable in {namespace}.")
            continue
        try:
            items = json.loads(result.stdout).get("items") or []
        except (json.JSONDecodeError, AttributeError):
            warnings.append(f"Kubernetes returned unreadable job status in {namespace}.")
            continue
        for item in items:
            run_id = ((item.get("metadata") or {}).get("labels") or {}).get(RUN_ID_LABEL)
            if run_id:
                states[run_id] = emulation_job_state(item)
    return states, warnings


def marker_state(access: S3Access, done_uri: str, failed_uri: str) -> str | None:
    """``failed`` wins over ``done``, as in the marker wait; None when neither exists."""
    if s3_object_exists(access, failed_uri):
        return "failed"
    if s3_object_exists(access, done_uri):
        return "done"
    return None


def run_markers(access: S3Access, artifact_uri: str, run_id: str, stages: Iterable[str]) -> dict[str, object]:
    """The emulation upload marker and the terminal marker of each named PBS stage."""
    prefix = run_prefix_uri(artifact_uri, run_id)
    return {
        "upload": marker_state(access, upload_marker_uri(prefix, "done"), upload_marker_uri(prefix, "failed")),
        "stages": {
            stage: marker_state(
                access, stage_marker_uri(prefix, stage, "done"), stage_marker_uri(prefix, stage, "failed")
            )
            for stage in stages
        },
    }
