"""Continue an S3 full-run whose driving process died (``full-run --resume-run``).

The original process keeps no state that is not also outside it: the upload
marker and the Kubernetes Job of the emulation, the ``.pbs/<stage>.jobid``
records and the stage markers. A resume reads them, keeps finished stages and
live jobs whose inputs are still coming, resubmits the rest of the graph and
waits as an ordinary full-run would. It never recreates the emulation: a run
whose emulation is gone without an upload has nothing to analyze.

Given the same configuration as the original run, a resume is idempotent: a
second resume finds the jobs the first one submitted and adopts them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.kubernetes import kubectl, resolve_kubeconfig, s3_emulation_job_name
from coinjoin_pipeline.execution.pbs.submission import pbs_job_probe, qdel_error, read_pbs_job_id
from coinjoin_pipeline.execution.pbs.validation import require_qsub
from coinjoin_pipeline.execution.probes import PROBE_QUEUED, PROBE_RUNNING, PROBE_UNKNOWN
from coinjoin_pipeline.execution.s3_submission import ResumedGraph, s3_full_run_plan_from_args, submit_s3_pbs_graph
from coinjoin_pipeline.execution.s3_workflow import (
    print_full_run_completed,
    wait_for_emulation_upload,
    wait_for_pbs_graph,
)
from coinjoin_pipeline.execution.stages import StageGraph
from coinjoin_pipeline.run_records import emulation_job_state, marker_state
from coinjoin_pipeline.storage.s3 import (
    S3Target,
    run_prefix_uri,
    s3_access_preflight,
    stage_marker_uri,
    upload_marker_uri,
)

LIVE = (PROBE_QUEUED, PROBE_RUNNING)


@dataclass(frozen=True)
class ResumeDecision:
    """Per-stage outcome of reading a run's records."""

    completed: frozenset[str] = frozenset()
    #: Live jobs kept as they are, by stage.
    adopted: dict[str, str] = field(default_factory=dict)
    #: Live jobs waiting for a dependency that is resubmitted; they would never start.
    cancel: dict[str, str] = field(default_factory=dict)
    resubmit: tuple[str, ...] = ()


def decide_resume(
    plan: StageGraph,
    markers: Mapping[str, str | None],
    jobs: Mapping[str, str],
    probes: Mapping[str, str],
) -> ResumeDecision:
    """Keep what still serves the graph and resubmit everything else, in graph order.

    A stage is finished when its ``done`` marker exists. A live job is kept when it
    has no ``failed`` marker and each PBS stage it depends on is finished or kept:
    a job that depends on a resubmitted stage still points at the old job ID, so
    PBS would never release it.
    """
    pbs_stages = {stage.name for stage in plan.scheduled()}
    completed: set[str] = set()
    adopted: dict[str, str] = {}
    cancel: dict[str, str] = {}
    resubmit: list[str] = []
    for stage in plan.scheduled():
        marker = markers.get(stage.name)
        if marker == "done":
            completed.add(stage.name)
            continue
        job_id = jobs.get(stage.name) if probes.get(stage.name) in LIVE else None
        inputs_kept = all(
            dependency in completed or dependency in adopted
            for dependency in stage.dependencies
            if dependency in pbs_stages
        )
        if job_id is not None and marker != "failed" and inputs_kept:
            adopted[stage.name] = job_id
            continue
        if job_id is not None:
            cancel[stage.name] = job_id
        resubmit.append(stage.name)
    return ResumeDecision(frozenset(completed), adopted, cancel, tuple(resubmit))


def _emulation_job(args: PipelineConfiguration) -> dict | None:
    """The run's emulation Job, None when Kubernetes says it does not exist."""
    kubeconfig = resolve_kubeconfig(args.kubernetes.kubeconfig)
    name = s3_emulation_job_name(required(args.run_id, "--run-id"))
    try:
        result = kubectl(kubeconfig, "get", "job", name, "-o", "json", namespace=args.kubernetes.namespace)
    except FileNotFoundError as error:
        raise RuntimeError("kubectl is required to resume a run whose emulation has not uploaded yet") from error
    if result.returncode != 0:
        if "notfound" in (result.stderr or "").lower().replace(" ", ""):
            return None
        raise RuntimeError(f"cannot inspect Kubernetes Job {name}: {(result.stderr or '').strip()}")
    return json.loads(result.stdout)


def _resume_emulation(args: PipelineConfiguration, run_prefix: str, target: S3Target) -> None:
    upload = marker_state(target.access, upload_marker_uri(run_prefix, "done"), upload_marker_uri(run_prefix, "failed"))
    if upload == "done":
        print("[resume] Emulation already uploaded its artifacts")
        return
    if upload == "failed":
        raise RuntimeError(
            f"the emulation of run {target.run_id} failed ({upload_marker_uri(run_prefix, 'failed')}); "
            "there is nothing to resume, start a new run"
        )
    job = _emulation_job(args)
    if job is None or emulation_job_state(job) in {"done", "failed"}:
        # The uploader writes a marker before the Job ends, so an ended Job without one lost its data.
        state = "is gone" if job is None else f"ended ({emulation_job_state(job)})"
        raise RuntimeError(
            f"the emulation Job of run {target.run_id} {state} without an upload marker; "
            "its artifacts are lost, start a new run"
        )
    if args.dry_run:
        print(f"[dry-run] Would keep waiting for {upload_marker_uri(run_prefix, 'done')}")
        return
    print("[resume] Emulation Job is still active; waiting for its upload")
    wait_for_emulation_upload(args, run_prefix, target.access)


def _read_stage_records(
    args: PipelineConfiguration, plan: StageGraph, target: S3Target, run_prefix: str
) -> ResumeDecision:
    run_dir = args.runs_path / target.run_id
    jobs = {stage.name: job_id for stage in plan.scheduled() if (job_id := read_pbs_job_id(run_dir, stage.name))}
    # Probe before reading markers: a job that ends in between has written its marker by then.
    probes = {stage: pbs_job_probe(job_id)() for stage, job_id in jobs.items()}
    unknown = sorted(stage for stage, state in probes.items() if state == PROBE_UNKNOWN)
    if unknown:
        raise RuntimeError(
            f"PBS cannot confirm the state of {', '.join(unknown)}; resuming now could run a stage twice, retry later"
        )
    markers = {
        stage.name: marker_state(
            target.access,
            stage_marker_uri(run_prefix, stage.name, "done"),
            stage_marker_uri(run_prefix, stage.name, "failed"),
        )
        for stage in plan.scheduled()
    }
    return decide_resume(plan, markers, jobs, probes)


def _report(decision: ResumeDecision) -> None:
    for stage in sorted(decision.completed):
        print(f"[resume] {stage}: finished, kept")
    for stage, job_id in decision.adopted.items():
        print(f"[resume] {stage}: PBS job {job_id} still active, adopted")
    for stage, job_id in decision.cancel.items():
        print(f"[resume] {stage}: PBS job {job_id} waits for a resubmitted stage, cancelling")
    for stage in decision.resubmit:
        print(f"[resume] {stage}: resubmitting")


def resume_s3_full_run(args: PipelineConfiguration) -> None:
    """Continue the S3 full-run of ``args.run_id`` from its records and wait for the report.

    The emulator digests are not pinned again: the emulation ran before this
    process, so the report flags them as missing instead of naming today's tags.
    """
    target = S3Target.from_args(args)
    run_prefix = run_prefix_uri(target.artifact_uri, target.run_id)
    plan = s3_full_run_plan_from_args(args)
    s3_access_preflight(target.access, target.artifact_uri)
    if plan.scheduled() and not args.dry_run:
        require_qsub()
    _resume_emulation(args, run_prefix, target)
    if not plan.scheduled():
        print(f"[full-run] Completed emulation only; artifacts under {run_prefix}/")
        return
    decision = _read_stage_records(args, plan, target, run_prefix)
    _report(decision)
    if args.dry_run:
        submit_s3_pbs_graph(args, ResumedGraph(decision.completed, decision.adopted))
        return
    for stage, job_id in decision.cancel.items():
        error = qdel_error(job_id)
        if error:
            raise RuntimeError(f"cannot cancel the stale {stage} job before resubmitting: {error}")
    jobs = submit_s3_pbs_graph(args, ResumedGraph(decision.completed, decision.adopted))
    wait_for_pbs_graph(args, plan, jobs, run_prefix, target.access)
    print_full_run_completed(run_prefix)
