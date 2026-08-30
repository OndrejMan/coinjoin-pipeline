"""High-level Kubernetes-to-S3-to-PBS full-run orchestration.

The module owns only ordering, marker waiting, and the deliberately asymmetric
cancellation policy — and it derives all three from the declared stage graph
rather than from a second, hand-maintained copy of the pipeline.  The wrapper
injects concrete Kubernetes, PBS, and S3 operations so it remains the
executable compatibility facade.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from client.artifacts import ArtifactTransportError, S3Access
from client.pbs import PBSError
from client.pbs_settings import stage_pbs_walltime
from client.stages import (
    StageGraph,
    StageKind,
    StagePlan,
    resource_group,
    s3_full_run_plan,
)


@dataclass(frozen=True)
class S3PBSJobs:
    """PBS job identifiers emitted by one submitted S3 analysis graph."""

    coinjoin_analysis: str | None = None
    coinjoin_mappings: str | None = None
    blocksci_parse: str | None = None
    blocksci_update: str | None = None
    blocksci_work: str | None = None
    unified_report: str | None = None

    @classmethod
    def from_plan(
        cls, plan: StageGraph, jobs: Mapping[str, str]
    ) -> "S3PBSJobs":
        """Collect the jobs submitted for one planned graph, keyed by kind."""
        by_kind = {
            stage.kind: jobs[stage.name] for stage in plan if stage.name in jobs
        }
        return cls(
            coinjoin_analysis=by_kind.get(StageKind.BASELINE),
            coinjoin_mappings=by_kind.get(StageKind.MAPPINGS),
            blocksci_parse=by_kind.get(StageKind.BLOCKSCI_PARSE),
            blocksci_update=by_kind.get(StageKind.BLOCKSCI_UPDATE),
            blocksci_work=by_kind.get(StageKind.BLOCKSCI_WORK),
            unified_report=by_kind.get(StageKind.REPORT),
        )

    def job_for(self, stage: StagePlan) -> str | None:
        """Return the job submitted for ``stage``, or ``None`` when skipped.

        Lookup is by stage kind rather than by stage name: one BlockSci work
        job carries the name of whichever task produced it (``blocksci``,
        ``blocksci-analyze``, ``blocksci-script``, …).
        """
        return {
            StageKind.BASELINE: self.coinjoin_analysis,
            StageKind.MAPPINGS: self.coinjoin_mappings,
            StageKind.BLOCKSCI_PARSE: self.blocksci_parse,
            StageKind.BLOCKSCI_UPDATE: self.blocksci_update,
            StageKind.BLOCKSCI_WORK: self.blocksci_work,
            StageKind.REPORT: self.unified_report,
        }.get(stage.kind)


@dataclass(frozen=True)
class S3FullRunOperations:
    """Concrete frontend operations used by :func:`run_s3_full_run`."""

    make_access: Callable[[argparse.Namespace], S3Access]
    require_qsub: Callable[[], None]
    stage_kubernetes_run: Callable[[argparse.Namespace, S3Access], None]
    run_kubernetes_emulation: Callable[[argparse.Namespace], None]
    wait_for_marker: Callable[..., None]
    kubernetes_probe: Callable[[Path, str, str], Callable[[], str]]
    collect_kubernetes_diagnostics: Callable[[Path, str, str], str]
    delete_kubernetes_job: Callable[[Path, str, str], None]
    kubernetes_job_name: Callable[[str], str]
    submit_pbs: Callable[[argparse.Namespace], S3PBSJobs]
    wait_for_pbs_stage: Callable[..., None]
    cancel_dependent_pbs_job: Callable[[str, str], bool]
    emulation_start_timeout: int


def s3_full_run_plan_from_args(args: argparse.Namespace) -> StageGraph:
    """Build the stage graph this invocation submits, waits for, and cancels."""
    return s3_full_run_plan(
        analysis_pbs=getattr(args, "analysisPbs", False),
        blocksci_pbs=getattr(args, "blocksciPbs", False),
        mappings_pbs=getattr(args, "mappingsPbs", False),
        blocksci_workflow=getattr(args, "blocksci_workflow", "combined"),
        blocksci_task=getattr(args, "blocksci_task", "detect"),
    )


def _wait_for_pbs_stage(
    *,
    operations: S3FullRunOperations,
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
        operations.wait_for_pbs_stage(
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
                operations.cancel_dependent_pbs_job(pending_stage.name, pending_job_id)
            else:
                print(
                    f"[full-run] {pending_stage.name} PBS job {pending_job_id} is left "
                    "running; its results still upload to the bucket "
                    f"(cancel with: qdel {pending_job_id})",
                    file=sys.stderr,
                )
        raise


def run_s3_full_run(args: argparse.Namespace, operations: S3FullRunOperations) -> None:
    """Run the canonical S3 full-run graph and wait for its terminal report."""
    access = operations.make_access(args)
    run_prefix = f"{args.artifact_uri}/{args.run_id}"
    kubeconfig_path = (
        Path(args.kubeconfig).expanduser().resolve()
        if args.kubeconfig
        else Path.home() / ".kube/config"
    )
    job_name = operations.kubernetes_job_name(args.run_id)
    plan = s3_full_run_plan_from_args(args)

    if args.dry_run:
        operations.run_kubernetes_emulation(args)
        print(
            f"[dry-run] Would wait for {run_prefix}/.k8s/upload.done "
            f"(timeout {args.emulation_timeout}s)"
        )
        operations.submit_pbs(args)
        for stage in plan.scheduled():
            print(f"[dry-run] Would wait for {run_prefix}/.pbs/{stage.name}.done")
        return

    operations.require_qsub()
    operations.stage_kubernetes_run(args, access)
    operations.run_kubernetes_emulation(args)
    print(f"[full-run] Waiting for emulation upload marker {run_prefix}/.k8s/upload.done")
    try:
        operations.wait_for_marker(
            "kubernetes-emulation",
            f"{run_prefix}/.k8s/upload.done",
            f"{run_prefix}/.k8s/upload.failed",
            access,
            timeout_seconds=args.emulation_timeout,
            start_timeout_seconds=operations.emulation_start_timeout,
            probe=operations.kubernetes_probe(kubeconfig_path, args.namespace, job_name),
        )
    except ArtifactTransportError:
        print(
            operations.collect_kubernetes_diagnostics(
                kubeconfig_path, args.namespace, job_name
            ),
            file=sys.stderr,
        )
        operations.delete_kubernetes_job(kubeconfig_path, args.namespace, job_name)
        print(
            f"[full-run] Requested deletion of failed Kubernetes Job {job_name} "
            "after collecting diagnostics.",
            file=sys.stderr,
        )
        raise

    jobs = operations.submit_pbs(args)
    submitted: list[tuple[StagePlan, str]] = [
        (stage, job_id)
        for stage in plan.scheduled()
        if (job_id := jobs.job_for(stage)) is not None
    ]
    for index, (stage, job_id) in enumerate(submitted):
        _wait_for_pbs_stage(
            operations=operations,
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
