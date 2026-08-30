"""State shared while one S3 PBS graph is being submitted."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from client.artifacts import ArtifactTransportError, S3Access, S3Target
from client.pbs import (
    DEFAULT_BLOCKSCI_IMAGE,
    DEFAULT_COINJOIN_ANALYSIS_IMAGE,
    DEFAULT_MAPPINGS_ENUMERATOR_IMAGE,
    DEFAULT_SAKE_IMAGE,
    DEFAULT_UNIFIED_REPORT_MEM,
    DEFAULT_UNIFIED_REPORT_NCPUS,
    DEFAULT_UNIFIED_REPORT_SCRATCH,
    DEFAULT_UNIFIED_REPORT_WALLTIME,
    PBSError,
    blocksci_analysis_pbs_command,
    blocksci_export_pbs_command,
    blocksci_external_report_pbs_command,
    blocksci_notebook_pbs_command,
    blocksci_parse_pbs_command,
    blocksci_pbs_command,
    blocksci_script_pbs_command,
    blocksci_update_pbs_command,
    coinjoin_analysis_pbs_command,
    persist_pbs_job_id,
)
from client.pbs_settings import (
    resolve_pbs_image,
    resolve_unified_report_pbs_image,
    resolve_unified_report_pbs_resource,
    resolve_uploader_image,
    stage_pbs_resources,
    unified_report_image_reference,
)
from client.s3_staging import pbs_stages_need_exporters
from client.s3_workflow import S3PBSJobs, s3_full_run_plan_from_args
from client.stages import combined_blocksci_exports_analysis


@dataclass(frozen=True)
class S3SubmissionOperations:
    """Marker operations resolved by the wrapper facade.

    Only the remote marker write stays injectable: it is a side effect on the
    bucket that the wrapper tests intercept.  Recording a job ID locally is a
    plain filesystem helper and is imported directly.
    """

    clear_stage_markers: Callable[[S3Access, str, str, str], None]


@dataclass
class S3SubmissionTracker:
    """Prepare marker state and remember every job for rollback on failure."""

    args: argparse.Namespace
    target: S3Target
    access: S3Access
    submitted_jobs: list[tuple[str, str]]
    operations: S3SubmissionOperations
    jobs: dict[str, str] = field(default_factory=dict)

    def prepare_stage(self, stage: str) -> None:
        """Clear this stage's stale remote terminal markers before qsub."""
        if self.args.dry_run:
            print(f"[dry-run] Would clear stale .pbs/{stage}.done|failed markers")
            return
        self.operations.clear_stage_markers(
            self.access,
            self.target.artifact_uri,
            self.target.run_id,
            stage,
        )

    def submit(self, stage: str, submit: Callable[[], str | None]) -> str | None:
        """Clear a stage's stale markers, submit it, and record the job.

        Every stage goes through here so no submission path can forget the
        marker reset that its wait depends on, or the bookkeeping that
        rollback and dependency wiring read back.
        """
        self.prepare_stage(stage)
        job_id = submit()
        self.record_job(stage, job_id)
        return job_id

    def record_job(self, stage: str, job_id: str | None) -> None:
        """Record submitted jobs in rollback and watch/overlap-detection state."""
        if not job_id:
            return
        self.submitted_jobs.append((stage, job_id))
        self.jobs[stage] = job_id
        submission_dir = getattr(self.args, "pbs_submission_dir", None)
        if submission_dir is not None:
            persist_pbs_job_id(Path(submission_dir), stage, job_id)


@dataclass(frozen=True)
class S3SubmissionLifecycleOperations:
    """Wrapper-resolved operations surrounding the concrete qsub graph."""

    compose_environment: Callable[[], Mapping[str, str]]
    submit_lock_path: Callable[[Path, str], Path]
    acquire_lock: Callable[[Path], object]
    ensure_no_active_submission: Callable[[Path], None]
    submit_graph: Callable[[argparse.Namespace, list[tuple[str, str]]], S3PBSJobs]
    rollback_submissions: Callable[[list[tuple[str, str]]], None]


@dataclass(frozen=True)
class S3StageSubmissionOperations:
    """The side effects the concrete S3 PBS graph performs.

    Only genuine boundaries are injected: bucket preflight/staging and the
    scheduler submissions themselves.  Pure helpers -- command construction,
    image and resource resolution, defaults -- are imported directly, so this
    bundle stays a list of what a caller must intercept rather than a second
    copy of the module's imports.  The graph has intentionally no dependency
    on ``client.wrapper``.
    """

    tracker_operations: S3SubmissionOperations
    s3_preflight: Callable[..., None]
    object_exists: Callable[..., bool]
    ensure_empty_prefix: Callable[..., None]
    ensure_exporters: Callable[..., None]
    submit_analysis: Callable[..., str | None]
    submit_mappings: Callable[..., str | None]
    submit_update: Callable[..., str | None]
    submit_blocksci: Callable[..., str | None]
    submit_parse: Callable[..., str | None]
    submit_blocksci_work: Callable[..., str | None]
    submit_report: Callable[..., str | None]


def submit_s3_pbs_graph(
    args: argparse.Namespace,
    operations: S3SubmissionLifecycleOperations,
) -> S3PBSJobs:
    """Serialise one S3 PBS submission and roll it back on submit failure."""
    if not args.dry_run:
        submission_dir = getattr(args, "pbs_submission_dir", None)
        if submission_dir is None:
            logs_root = Path(
                operations.compose_environment().get("EMULATION_LOGS_DIR", ".")
            ).expanduser().resolve()
            submission_dir = operations.submit_lock_path(logs_root, args.run_id).parent
            args.pbs_submission_dir = submission_dir
        submission_dir = Path(submission_dir)
        operations.acquire_lock(submission_dir / ".pbs-submit.lock")
        operations.ensure_no_active_submission(submission_dir)

    submitted_jobs: list[tuple[str, str]] = []
    try:
        return operations.submit_graph(args, submitted_jobs)
    except BaseException:
        operations.rollback_submissions(submitted_jobs)
        raise


def submit_s3_pbs_stages(
    args: argparse.Namespace,
    submitted_jobs: list[tuple[str, str]],
    operations: S3StageSubmissionOperations,
) -> S3PBSJobs:
    """Submit the concrete S3 PBS DAG while preserving marker semantics."""
    target = S3Target.from_args(args)
    access = target.access
    plan = s3_full_run_plan_from_args(args)
    tracker = S3SubmissionTracker(
        args,
        target,
        access,
        submitted_jobs,
        operations.tracker_operations,
    )
    common = dict(target=target, dry_run=args.dry_run)
    analysis_resources = stage_pbs_resources(args, "analysis")
    mappings_resources = stage_pbs_resources(args, "mappings")
    workflow = getattr(args, "blocksci_workflow", "combined")
    task = getattr(args, "blocksci_task", "detect")
    if task == "update" and not args.dry_run:
        source_run_id = args.blocksci_cache_source_run_id
        operations.s3_preflight(access, target.artifact_uri)
        if not operations.object_exists(
            access,
            f"{target.artifact_uri}/{source_run_id}/blocksci-parse_data/manifest.json",
        ):
            raise ArtifactTransportError(
                f"source BlockSci cache manifest does not exist for run {source_run_id}"
            )
        operations.ensure_empty_prefix(access, target.artifact_uri, target.run_id)
    mappings_pbs = getattr(args, "mappingsPbs", False)
    if (
        not args.dry_run
        and not args.analysisPbs
        and (mappings_pbs or (args.blocksciPbs and task == "detect"))
    ):
        operations.s3_preflight(access, target.artifact_uri)
        if not operations.object_exists(
            access,
            f"{target.artifact_uri}/{target.run_id}/coinjoin-analysis_data/coinjoin_tx_info.json",
        ):
            raise ArtifactTransportError(
                "resuming without --analysisPbs requires an existing "
                f"coinjoin-analysis_data/coinjoin_tx_info.json for run {target.run_id}"
            )
    if not args.dry_run and (
        getattr(args, "stage_exporters", False) or pbs_stages_need_exporters(args)
    ):
        operations.ensure_exporters(args)
    separate_combined_report = combined_blocksci_exports_analysis(
        analysis_pbs=args.analysisPbs,
        blocksci_pbs=args.blocksciPbs,
        mappings_pbs=mappings_pbs,
        blocksci_task=task,
    )
    if args.analysisPbs:
        tracker.submit(
            "coinjoin-analysis",
            lambda: operations.submit_analysis(
                **common,
                image=resolve_pbs_image(
                    args, DEFAULT_COINJOIN_ANALYSIS_IMAGE, "pbs_coinjoin_analysis_image"
                ),
                command=coinjoin_analysis_pbs_command("collect_docker"),
                **analysis_resources,
            ),
        )
    if mappings_pbs:
        tracker.submit(
            "coinjoin-mappings",
            lambda: operations.submit_mappings(
                **common,
                enumerator_image=resolve_pbs_image(
                    args,
                    DEFAULT_MAPPINGS_ENUMERATOR_IMAGE,
                    "pbs_mappings_enumerator_image",
                ),
                sake_image=resolve_pbs_image(args, DEFAULT_SAKE_IMAGE, "pbs_sake_image"),
                mining_fee_rate=getattr(args, "mapping_mining_fee_rate", 1),
                coordination_fee_rate=getattr(args, "mapping_coordination_fee_rate", 0.003),
                max_decomposition_fee=getattr(args, "mapping_max_decomposition_fee", 6000),
                mode=getattr(args, "mapping_mode", "numeric"),
                timeout=getattr(args, "mapping_timeout", 60),
                retry_timeout=getattr(args, "mapping_retry_timeout", 600),
                sake_seed=getattr(args, "sake_seed", 20260704),
                **mappings_resources,
                dependency_job_id=plan.dependency_id(
                        "coinjoin-mappings", tracker.jobs
                    ),
            ),
        )
    if args.blocksciPbs:
        blocksci_resources = stage_pbs_resources(args, "blocksci")
        blocksci_image = resolve_pbs_image(
            args, DEFAULT_BLOCKSCI_IMAGE, "pbs_blocksci_image"
        )
        if task == "update":
            tracker.submit(
                "blocksci-update",
                lambda: operations.submit_update(
                    **common,
                    source_run_id=args.blocksci_cache_source_run_id,
                    image=blocksci_image,
                    command=blocksci_update_pbs_command(target.run_id),
                    external_bitcoin_datadir=Path(args.blocksci_external_bitcoin_datadir),
                    external_network=args.blocksci_network,
                    external_max_block=args.blocksci_max_block,
                    **blocksci_resources,
                ),
            )
        elif workflow == "combined":
            tracker.submit(
                "blocksci",
                lambda: operations.submit_blocksci(
                    **common,
                    image=blocksci_image,
                    command=blocksci_pbs_command(
                        target.run_id,
                        args.coinjoin_type,
                        args.min_input_count,
                        args.joinmarket_detector,
                        args.joinmarket_min_base_fee,
                        args.joinmarket_percentage_fee,
                        args.joinmarket_max_depth,
                        include_report=not separate_combined_report,
                        export_analysis=separate_combined_report,
                    ),
                    **blocksci_resources,
                    include_report=not separate_combined_report,
                    export_analysis=separate_combined_report,
                ),
            )
        else:
            if workflow == "reusable":
                external_bitcoin = getattr(args, "blocksci_external_bitcoin_datadir", None)
                bitcoin_blocks_uri = getattr(args, "blocksci_bitcoin_blocks_uri", None)
                external_index = getattr(args, "blocksci_external_blocksci_dir", None)
                parse_command = blocksci_parse_pbs_command(target.run_id)
                if external_bitcoin or bitcoin_blocks_uri:
                    parse_command = blocksci_parse_pbs_command(
                        target.run_id,
                        coin_type=args.blocksci_network,
                        disk_path="/mnt/data",
                        max_block_expression=str(args.blocksci_max_block + 1),
                    )
                tracker.submit(
                    "blocksci-parse",
                    lambda: operations.submit_parse(
                        **common,
                        image=blocksci_image,
                        command=parse_command,
                        external_bitcoin_datadir=(
                            Path(external_bitcoin) if external_bitcoin else None
                        ),
                        bitcoin_blocks_uri=bitcoin_blocks_uri,
                        external_blocksci_dir=(Path(external_index) if external_index else None),
                        external_network=getattr(args, "blocksci_network", None),
                        external_max_block=getattr(args, "blocksci_max_block", None),
                        **blocksci_resources,
                    ),
                )
            if task != "parse":
                mode = f"blocksci-{task if task != 'detect' else 'analyze'}"
                if task == "detect":
                    work_command = blocksci_analysis_pbs_command(
                        target.run_id,
                        args.coinjoin_type,
                        args.min_input_count,
                        args.joinmarket_detector,
                        args.joinmarket_min_base_fee,
                        args.joinmarket_percentage_fee,
                        args.joinmarket_max_depth,
                    )
                elif task == "external":
                    work_command = blocksci_external_report_pbs_command(
                        target.run_id,
                        args.coinjoin_type,
                        args.min_input_count,
                        args.joinmarket_detector,
                        args.joinmarket_min_base_fee,
                        args.joinmarket_percentage_fee,
                        args.joinmarket_max_depth,
                    )
                elif task == "script":
                    work_command = blocksci_script_pbs_command(
                        target.run_id,
                        args.coinjoin_type,
                        args.min_input_count,
                        args.joinmarket_detector,
                        args.joinmarket_min_base_fee,
                        args.joinmarket_percentage_fee,
                        args.joinmarket_max_depth,
                    )
                else:
                    work_command = blocksci_notebook_pbs_command(
                        getattr(args, "blocksci_notebook_port", None) or 8888
                    )
                tracker.submit(
                    mode,
                    lambda: operations.submit_blocksci_work(
                        **common,
                        image=blocksci_image,
                        command=work_command,
                        mode=mode,
                        user_script=Path(args.blocksci_script) if task == "script" else None,
                        external_baseline_uri=(
                            args.external_baseline_uri if task == "external" else None
                        ),
                        notebooks_dir=(
                            Path(args.blocksci_notebooks_dir)
                            if task == "notebook"
                            and getattr(args, "blocksci_notebooks_dir", None)
                            else None
                        ),
                        notebook_port=getattr(args, "blocksci_notebook_port", None) or 8888,
                        dependency_job_id=plan.dependency_id(mode, tracker.jobs),
                        **blocksci_resources,
                    ),
                )
    report_stage = plan.get("unified-report")
    if report_stage is not None:
        dependency_job_ids = plan.dependency_ids("unified-report", tracker.jobs)
        if not args.dry_run and len(dependency_job_ids) != len(report_stage.dependencies):
            raise PBSError("Could not obtain analyzer job IDs for the unified report dependency")
        tracker.submit(
            "unified-report",
            lambda: operations.submit_report(
                **common,
                image=resolve_unified_report_pbs_image(args),
                command=blocksci_export_pbs_command(
                    target.run_id,
                    args.coinjoin_type,
                    args.min_input_count,
                    args.joinmarket_detector,
                    args.joinmarket_min_base_fee,
                    args.joinmarket_percentage_fee,
                    args.joinmarket_max_depth,
                    uploader_image=resolve_uploader_image(args),
                    unified_report_image=unified_report_image_reference(args),
                ),
                ncpus=resolve_unified_report_pbs_resource(
                    args, "ncpus", DEFAULT_UNIFIED_REPORT_NCPUS
                ),
                mem=resolve_unified_report_pbs_resource(
                    args, "mem", DEFAULT_UNIFIED_REPORT_MEM
                ),
                scratch=resolve_unified_report_pbs_resource(
                    args, "scratch", DEFAULT_UNIFIED_REPORT_SCRATCH
                ),
                walltime=resolve_unified_report_pbs_resource(
                    args, "walltime", DEFAULT_UNIFIED_REPORT_WALLTIME
                ),
                dependency_job_ids=dependency_job_ids,
                include_mappings=mappings_pbs,
            ),
        )
    return S3PBSJobs.from_plan(plan, tracker.jobs)
