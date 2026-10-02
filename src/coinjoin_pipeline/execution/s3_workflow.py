"""High-level Kubernetes-to-S3-to-PBS full-run orchestration.

The module owns only ordering, marker waiting, and the deliberately asymmetric
cancellation policy — and it derives all three from the declared stage graph
rather than from a second, hand-maintained copy of the pipeline.
"""

from __future__ import annotations

import sys

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.kubernetes import (
    S3_JOB_START_TIMEOUT_SECONDS,
    collect_s3_emulation_diagnostics,
    delete_s3_emulation_job,
    kubernetes_job_probe,
    resolve_kubeconfig,
    s3_emulation_job_name,
)
from coinjoin_pipeline.execution.pbs.validation import PBSError, require_qsub
from coinjoin_pipeline.execution.pbs_settings import stage_pbs_walltime
from coinjoin_pipeline.execution.s3_emulation import run_s3_kubernetes_emulation
from coinjoin_pipeline.execution.s3_markers import cancel_dependent_pbs_job, wait_for_s3_pbs_stage
from coinjoin_pipeline.execution.s3_staging import stage_kubernetes_s3_run
from coinjoin_pipeline.execution.s3_submission import s3_full_run_plan_from_args, submit_s3_pbs_graph
from coinjoin_pipeline.execution.stages import StageGraph, StagePlan, resource_group
from coinjoin_pipeline.storage.s3 import ArtifactTransportError, S3Access, S3Target, wait_for_s3_marker


def _wait_for_pbs_stage(
    *,
    plan: StageGraph,
    stage: StagePlan,
    job_id: str,
    pending: tuple[tuple[StagePlan, str], ...],
    run_prefix: str,
    access: S3Access,
    walltime: str,
) -> None:
    """Wait for a PBS marker and cancel only the stages the graph blocks.

    A failure invalidates exactly the transitive dependents of the failed
    stage.  Everything else still queued is deliberately left running: those
    jobs publish artifacts of their own, so cancelling them would throw away
    work the failure did not affect.
    """
    try:
        wait_for_s3_pbs_stage(
            stage=stage.name,
            job_id=job_id,
            run_prefix=run_prefix,
            access=access,
            walltime=walltime,
        )
    except (ArtifactTransportError, PBSError):
        blocked = {dependent.name for dependent in plan.dependents_of(stage.name)}
        for pending_stage, pending_job_id in pending:
            if pending_stage.name in blocked:
                cancel_dependent_pbs_job(pending_stage.name, pending_job_id)
            else:
                print(
                    f"[full-run] {pending_stage.name} PBS job {pending_job_id} is left "
                    "running; its results still upload to the bucket "
                    f"(cancel with: qdel {pending_job_id})",
                    file=sys.stderr,
                )
        raise


def run_s3_full_run(args: PipelineConfiguration) -> None:
    """Run the canonical S3 full-run graph and wait for its terminal report."""
    target = S3Target.from_args(args)
    access = target.access
    run_prefix = f"{target.artifact_uri}/{target.run_id}"
    kubeconfig_path = resolve_kubeconfig(args.kubernetes.kubeconfig)
    job_name = s3_emulation_job_name(target.run_id)
    plan = s3_full_run_plan_from_args(args)

    if args.dry_run:
        run_s3_kubernetes_emulation(args)
        print(f"[dry-run] Would wait for {run_prefix}/.k8s/upload.done (timeout {args.emulation_timeout}s)")
        submit_s3_pbs_graph(args)
        for stage in plan.scheduled():
            print(f"[dry-run] Would wait for {run_prefix}/.pbs/{stage.name}.done")
        return

    require_qsub()
    stage_kubernetes_s3_run(args)
    run_s3_kubernetes_emulation(args)
    print(f"[full-run] Waiting for emulation upload marker {run_prefix}/.k8s/upload.done")
    try:
        wait_for_s3_marker(
            "kubernetes-emulation",
            f"{run_prefix}/.k8s/upload.done",
            f"{run_prefix}/.k8s/upload.failed",
            access,
            timeout_seconds=args.emulation_timeout,
            start_timeout_seconds=S3_JOB_START_TIMEOUT_SECONDS,
            probe=kubernetes_job_probe(kubeconfig_path, args.kubernetes.namespace, job_name),
        )
    except ArtifactTransportError:
        print(
            collect_s3_emulation_diagnostics(kubeconfig_path, args.kubernetes.namespace, job_name),
            file=sys.stderr,
        )
        delete_s3_emulation_job(kubeconfig_path, args.kubernetes.namespace, job_name)
        print(
            f"[full-run] Requested deletion of failed Kubernetes Job {job_name} after collecting diagnostics.",
            file=sys.stderr,
        )
        raise

    jobs = submit_s3_pbs_graph(args)
    submitted: list[tuple[StagePlan, str]] = [
        (stage, job_id) for stage in plan.scheduled() if (job_id := jobs.job_for(stage)) is not None
    ]
    for index, (stage, job_id) in enumerate(submitted):
        _wait_for_pbs_stage(
            plan=plan,
            stage=stage,
            job_id=job_id,
            pending=tuple(submitted[index + 1 :]),
            run_prefix=run_prefix,
            access=access,
            walltime=stage_pbs_walltime(args, resource_group(stage.kind)),
        )
    print(
        f"[full-run] Completed; results under {run_prefix}/ "
        "(coinjoin-analysis_data/, blocksci-analysis_data/, "
        "coinjoin-mappings_data/ when requested, blocksci-parse_data/ when reusable, "
        "coinjoinPipeline_data/, logs/)"
    )
