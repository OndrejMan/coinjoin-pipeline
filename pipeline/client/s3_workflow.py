"""High-level Kubernetes-to-S3-to-PBS full-run orchestration.

The module owns only ordering, marker waiting, and the deliberately asymmetric
cancellation policy.  The wrapper injects concrete Kubernetes, PBS, and S3
operations so it remains the executable compatibility facade while the lower
level S3 submission code is extracted in later steps.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from client.artifacts import ArtifactTransportError, S3Access
from client.pbs import PBSError
from client.stages import s3_full_run_plan


@dataclass(frozen=True)
class S3PBSJobs:
    """PBS job identifiers emitted by one submitted S3 analysis graph."""

    coinjoin_analysis: str | None = None
    coinjoin_mappings: str | None = None
    blocksci_parse: str | None = None
    blocksci_update: str | None = None
    blocksci_work: str | None = None
    unified_report: str | None = None


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
    analysis_walltime: Callable[[argparse.Namespace], str]
    mappings_walltime: Callable[[argparse.Namespace], str]
    blocksci_walltime: Callable[[argparse.Namespace], str]
    report_walltime: Callable[[argparse.Namespace], str]
    emulation_start_timeout: int


def _wait_for_pbs_stage(
    *,
    operations: S3FullRunOperations,
    stage: str,
    job_id: str,
    run_prefix: str,
    access: S3Access,
    walltime: str,
    dependent_jobs: tuple[tuple[str, str | None], ...] = (),
    independent_job: str | None = None,
) -> None:
    """Wait for a PBS marker and cancel only declared dependent jobs on failure."""
    try:
        operations.wait_for_pbs_stage(
            stage=stage,
            job_id=job_id,
            run_prefix=run_prefix,
            access=access,
            walltime=walltime,
        )
    except (ArtifactTransportError, PBSError):
        for dependent_stage, dependent_job_id in dependent_jobs:
            if dependent_job_id:
                operations.cancel_dependent_pbs_job(dependent_stage, dependent_job_id)
        if independent_job:
            print(
                f"[full-run] BlockSci work PBS job {independent_job} is left running; "
                "its results still upload to the bucket "
                f"(cancel with: qdel {independent_job})",
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
    stage_plan = s3_full_run_plan(
        mappings_pbs=getattr(args, "mappingsPbs", False),
        blocksci_workflow=getattr(args, "blocksci_workflow", "combined"),
    )

    if args.dry_run:
        operations.run_kubernetes_emulation(args)
        print(
            f"[dry-run] Would wait for {run_prefix}/.k8s/upload.done "
            f"(timeout {args.emulation_timeout}s)"
        )
        operations.submit_pbs(args)
        for stage in stage_plan[1:]:
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
    analysis_walltime = operations.analysis_walltime(args)
    mappings_walltime = operations.mappings_walltime(args)
    blocksci_walltime = operations.blocksci_walltime(args)
    report_walltime = operations.report_walltime(args)
    if jobs.coinjoin_analysis:
        _wait_for_pbs_stage(
            operations=operations,
            stage="coinjoin-analysis",
            job_id=jobs.coinjoin_analysis,
            run_prefix=run_prefix,
            access=access,
            walltime=analysis_walltime,
            dependent_jobs=(
                ("coinjoin-mappings", jobs.coinjoin_mappings),
                ("unified-report", jobs.unified_report),
            ),
            independent_job=jobs.blocksci_work,
        )
    if jobs.blocksci_parse:
        _wait_for_pbs_stage(
            operations=operations,
            stage="blocksci-parse",
            job_id=jobs.blocksci_parse,
            run_prefix=run_prefix,
            access=access,
            walltime=blocksci_walltime,
            dependent_jobs=(
                ("BlockSci work", jobs.blocksci_work),
                ("unified-report", jobs.unified_report),
            ),
        )
    if jobs.blocksci_work:
        blocksci_stage = "blocksci-analyze" if jobs.blocksci_parse else "blocksci"
        _wait_for_pbs_stage(
            operations=operations,
            stage=blocksci_stage,
            job_id=jobs.blocksci_work,
            run_prefix=run_prefix,
            access=access,
            walltime=blocksci_walltime,
            dependent_jobs=(("unified-report", jobs.unified_report),),
        )
    if jobs.coinjoin_mappings:
        _wait_for_pbs_stage(
            operations=operations,
            stage="coinjoin-mappings",
            job_id=jobs.coinjoin_mappings,
            run_prefix=run_prefix,
            access=access,
            walltime=mappings_walltime,
            dependent_jobs=(("unified-report", jobs.unified_report),),
        )
    if jobs.unified_report:
        operations.wait_for_pbs_stage(
            stage="unified-report",
            job_id=jobs.unified_report,
            run_prefix=run_prefix,
            access=access,
            walltime=report_walltime,
        )
    print(
        f"[full-run] Completed; results under {run_prefix}/ "
        "(coinjoin-analysis_data/, blocksci-analysis_data/, "
        "coinjoin-mappings_data/ when requested, blocksci-parse_data/ when reusable, "
        "coinjoinPipeline_data/, logs/)"
    )
