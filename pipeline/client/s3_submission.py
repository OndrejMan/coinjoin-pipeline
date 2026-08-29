"""State shared while one S3 PBS graph is being submitted."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from client.artifacts import ArtifactTransportError, S3Access, S3Target
from client.pbs import PBSError
from client.pbs_settings import PBSResources
from client.s3_workflow import S3PBSJobs


@dataclass(frozen=True)
class S3SubmissionOperations:
    """Marker and job-recording operations resolved by the wrapper facade."""

    clear_stage_markers: Callable[[S3Access, str, str, str], None]
    persist_job_id: Callable[[Path, str, str], None]


@dataclass
class S3SubmissionTracker:
    """Prepare marker state and remember every job for rollback on failure."""

    args: argparse.Namespace
    target: S3Target
    access: S3Access
    submitted_jobs: list[tuple[str, str]]
    operations: S3SubmissionOperations

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

    def record_job(self, stage: str, job_id: str | None) -> None:
        """Record submitted jobs in rollback and watch/overlap-detection state."""
        if not job_id:
            return
        self.submitted_jobs.append((stage, job_id))
        submission_dir = getattr(self.args, "pbs_submission_dir", None)
        if submission_dir is not None:
            self.operations.persist_job_id(Path(submission_dir), stage, job_id)


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
    """All wrapper-resolved dependencies of the concrete S3 PBS graph.

    The graph has intentionally no dependency on ``client.wrapper``.  The
    wrapper builds this bundle from its historical names, retaining the
    existing monkeypatch surface while this module owns graph construction.
    """

    make_access: Callable[[S3Target], S3Access]
    tracker_operations: S3SubmissionOperations
    stage_resources: Callable[..., PBSResources]
    s3_preflight: Callable[..., None]
    object_exists: Callable[..., bool]
    ensure_empty_prefix: Callable[..., None]
    stages_need_exporters: Callable[..., bool]
    ensure_exporters: Callable[..., None]
    resolve_image: Callable[..., str]
    analysis_command: Callable[..., str]
    submit_analysis: Callable[..., str | None]
    submit_mappings: Callable[..., str | None]
    update_command: Callable[..., str]
    submit_update: Callable[..., str | None]
    blocksci_command: Callable[..., str]
    submit_blocksci: Callable[..., str | None]
    parse_command: Callable[..., str]
    submit_parse: Callable[..., str | None]
    blocksci_analysis_command: Callable[..., str]
    blocksci_external_command: Callable[..., str]
    blocksci_script_command: Callable[..., str]
    blocksci_notebook_command: Callable[..., str]
    submit_blocksci_work: Callable[..., str | None]
    resolve_report_image: Callable[..., str]
    report_command: Callable[..., str]
    report_resource: Callable[..., int | str]
    resolve_uploader_image: Callable[..., str]
    unified_report_image_reference: Callable[..., str]
    submit_report: Callable[..., str | None]
    default_analysis_image: str
    default_blocksci_image: str
    default_mappings_enumerator_image: str
    default_sake_image: str
    default_report_ncpus: int
    default_report_mem: str
    default_report_scratch: str
    default_report_walltime: str


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
    access = operations.make_access(target)
    tracker = S3SubmissionTracker(
        args,
        target,
        access,
        submitted_jobs,
        operations.tracker_operations,
    )
    common = dict(target=target, dry_run=args.dry_run)
    analysis_resources = operations.stage_resources(args, "analysis")
    mappings_resources = operations.stage_resources(args, "mappings")
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
        getattr(args, "stage_exporters", False) or operations.stages_need_exporters(args)
    ):
        operations.ensure_exporters(args)
    separate_combined_report = (
        args.blocksciPbs and task == "detect" and (args.analysisPbs or mappings_pbs)
    )
    analysis_job_id = None
    mappings_job_id = None
    blocksci_parse_job_id = None
    blocksci_update_job_id = None
    blocksci_work_job_id = None
    if args.analysisPbs:
        tracker.prepare_stage("coinjoin-analysis")
        analysis_job_id = operations.submit_analysis(
            **common,
            image=operations.resolve_image(
                args, operations.default_analysis_image, "pbs_coinjoin_analysis_image"
            ),
            command=operations.analysis_command("collect_docker"),
            **analysis_resources,
        )
        tracker.record_job("coinjoin-analysis", analysis_job_id)
    if mappings_pbs:
        tracker.prepare_stage("coinjoin-mappings")
        mappings_job_id = operations.submit_mappings(
            **common,
            enumerator_image=operations.resolve_image(
                args,
                operations.default_mappings_enumerator_image,
                "pbs_mappings_enumerator_image",
            ),
            sake_image=operations.resolve_image(
                args, operations.default_sake_image, "pbs_sake_image"
            ),
            mining_fee_rate=getattr(args, "mapping_mining_fee_rate", 1),
            coordination_fee_rate=getattr(args, "mapping_coordination_fee_rate", 0.003),
            max_decomposition_fee=getattr(args, "mapping_max_decomposition_fee", 6000),
            mode=getattr(args, "mapping_mode", "numeric"),
            timeout=getattr(args, "mapping_timeout", 60),
            retry_timeout=getattr(args, "mapping_retry_timeout", 600),
            sake_seed=getattr(args, "sake_seed", 20260704),
            **mappings_resources,
            dependency_job_id=analysis_job_id,
        )
        tracker.record_job("coinjoin-mappings", mappings_job_id)
    if args.blocksciPbs:
        blocksci_resources = operations.stage_resources(args, "blocksci")
        blocksci_image = operations.resolve_image(
            args, operations.default_blocksci_image, "pbs_blocksci_image"
        )
        if task == "update":
            tracker.prepare_stage("blocksci-update")
            blocksci_update_job_id = operations.submit_update(
                **common,
                source_run_id=args.blocksci_cache_source_run_id,
                image=blocksci_image,
                command=operations.update_command(target.run_id),
                external_bitcoin_datadir=Path(args.blocksci_external_bitcoin_datadir),
                external_network=args.blocksci_network,
                external_max_block=args.blocksci_max_block,
                **blocksci_resources,
            )
            tracker.record_job("blocksci-update", blocksci_update_job_id)
        elif workflow == "combined":
            tracker.prepare_stage("blocksci")
            blocksci_work_job_id = operations.submit_blocksci(
                **common,
                image=blocksci_image,
                command=operations.blocksci_command(
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
            )
            tracker.record_job("blocksci", blocksci_work_job_id)
        else:
            if workflow == "reusable":
                external_bitcoin = getattr(args, "blocksci_external_bitcoin_datadir", None)
                bitcoin_blocks_uri = getattr(args, "blocksci_bitcoin_blocks_uri", None)
                external_index = getattr(args, "blocksci_external_blocksci_dir", None)
                parse_command = operations.parse_command(target.run_id)
                if external_bitcoin or bitcoin_blocks_uri:
                    parse_command = operations.parse_command(
                        target.run_id,
                        coin_type=args.blocksci_network,
                        disk_path="/mnt/data",
                        max_block_expression=str(args.blocksci_max_block + 1),
                    )
                tracker.prepare_stage("blocksci-parse")
                blocksci_parse_job_id = operations.submit_parse(
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
                )
                tracker.record_job("blocksci-parse", blocksci_parse_job_id)
            if task != "parse":
                mode = f"blocksci-{task if task != 'detect' else 'analyze'}"
                if task == "detect":
                    work_command = operations.blocksci_analysis_command(
                        target.run_id,
                        args.coinjoin_type,
                        args.min_input_count,
                        args.joinmarket_detector,
                        args.joinmarket_min_base_fee,
                        args.joinmarket_percentage_fee,
                        args.joinmarket_max_depth,
                    )
                elif task == "external":
                    work_command = operations.blocksci_external_command(
                        target.run_id,
                        args.coinjoin_type,
                        args.min_input_count,
                        args.joinmarket_detector,
                        args.joinmarket_min_base_fee,
                        args.joinmarket_percentage_fee,
                        args.joinmarket_max_depth,
                    )
                elif task == "script":
                    work_command = operations.blocksci_script_command(
                        target.run_id,
                        args.coinjoin_type,
                        args.min_input_count,
                        args.joinmarket_detector,
                        args.joinmarket_min_base_fee,
                        args.joinmarket_percentage_fee,
                        args.joinmarket_max_depth,
                    )
                else:
                    work_command = operations.blocksci_notebook_command(
                        getattr(args, "blocksci_notebook_port", None) or 8888
                    )
                tracker.prepare_stage(mode)
                blocksci_work_job_id = operations.submit_blocksci_work(
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
                    dependency_job_id=blocksci_parse_job_id,
                    **blocksci_resources,
                )
                tracker.record_job(mode, blocksci_work_job_id)
    report_job_id = None
    needs_decoupled_report = task == "detect" and args.blocksciPbs and (
        separate_combined_report or workflow != "combined"
    )
    if needs_decoupled_report:
        dependency_job_ids = tuple(
            job_id
            for job_id in (analysis_job_id, blocksci_work_job_id, mappings_job_id)
            if job_id is not None
        )
        expected_dependencies = (
            int(args.analysisPbs) + int(args.blocksciPbs) + int(mappings_pbs)
        )
        if not args.dry_run and len(dependency_job_ids) != expected_dependencies:
            raise PBSError("Could not obtain analyzer job IDs for the unified report dependency")
        tracker.prepare_stage("unified-report")
        report_job_id = operations.submit_report(
            **common,
            image=operations.resolve_report_image(args),
            command=operations.report_command(
                target.run_id,
                args.coinjoin_type,
                args.min_input_count,
                args.joinmarket_detector,
                args.joinmarket_min_base_fee,
                args.joinmarket_percentage_fee,
                args.joinmarket_max_depth,
                uploader_image=operations.resolve_uploader_image(args),
                unified_report_image=operations.unified_report_image_reference(args),
            ),
            ncpus=operations.report_resource(
                args, "ncpus", operations.default_report_ncpus
            ),
            mem=operations.report_resource(args, "mem", operations.default_report_mem),
            scratch=operations.report_resource(
                args, "scratch", operations.default_report_scratch
            ),
            walltime=operations.report_resource(
                args, "walltime", operations.default_report_walltime
            ),
            dependency_job_ids=dependency_job_ids,
            include_mappings=mappings_pbs,
        )
        tracker.record_job("unified-report", report_job_id)
    return S3PBSJobs(
        coinjoin_analysis=analysis_job_id,
        coinjoin_mappings=mappings_job_id,
        blocksci_parse=blocksci_parse_job_id,
        blocksci_update=blocksci_update_job_id,
        blocksci_work=blocksci_work_job_id,
        unified_report=report_job_id,
    )
