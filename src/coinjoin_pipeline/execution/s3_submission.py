"""State shared while one S3 PBS graph is being submitted."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from exporters.artifact_paths import BASELINE_FILE, BLOCKSCI_PARSE_DIR

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.pbs.commands import (
    blocksci_analysis_pbs_command,
    blocksci_export_pbs_command,
    blocksci_external_report_pbs_command,
    blocksci_notebook_pbs_command,
    blocksci_parse_pbs_command,
    blocksci_pbs_command,
    blocksci_script_pbs_command,
    blocksci_update_pbs_command,
    coinjoin_analysis_pbs_command,
)
from coinjoin_pipeline.execution.pbs.defaults import (
    DEFAULT_BLOCKSCI_IMAGE,
    DEFAULT_COINJOIN_ANALYSIS_IMAGE,
    DEFAULT_MAPPINGS_ENUMERATOR_IMAGE,
    DEFAULT_SAKE_IMAGE,
    DEFAULT_UNIFIED_REPORT_MEM,
    DEFAULT_UNIFIED_REPORT_NCPUS,
    DEFAULT_UNIFIED_REPORT_SCRATCH,
    DEFAULT_UNIFIED_REPORT_WALLTIME,
)
from coinjoin_pipeline.execution.locks import acquire_lock, ensure_no_active_s3_pbs_submission, pbs_submit_lock_path
from coinjoin_pipeline.execution.pbs.submission import PBSJobSpec, persist_pbs_job_id, submit_pbs_job
from coinjoin_pipeline.execution.pbs.templates_s3 import (
    render_blocksci_analyze_s3_pbs,
    render_blocksci_parse_s3_pbs,
    render_blocksci_s3_pbs,
    render_blocksci_update_s3_pbs,
    render_coinjoin_analysis_s3_pbs,
    render_mappings_s3_pbs,
    render_unified_report_s3_pbs,
)
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pbs_settings import (
    PBSResources,
    resolve_pbs_image,
    resolve_unified_report_pbs_image,
    resolve_unified_report_pbs_resource,
    resolve_uploader_image,
    stage_pbs_resources,
    unified_report_image_reference,
)
from coinjoin_pipeline.execution.s3_markers import rollback_s3_pbs_submissions
from coinjoin_pipeline.execution.s3_staging import ensure_staged_exporters, pbs_stages_need_exporters
from coinjoin_pipeline.execution.stages import (
    StageGraph,
    StageKind,
    StagePlan,
    s3_full_run_plan,
)
from coinjoin_pipeline.storage.s3 import (
    ArtifactTransportError,
    S3Access,
    S3Target,
    clear_s3_stage_markers,
    ensure_empty_run_prefix,
    s3_access_preflight,
    s3_object_exists,
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
    def from_plan(cls, plan: StageGraph, jobs: Mapping[str, str]) -> S3PBSJobs:
        """Collect the jobs submitted for one planned graph, keyed by kind."""
        by_kind = {stage.kind: jobs[stage.name] for stage in plan if stage.name in jobs}
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


def s3_full_run_plan_from_args(args: PipelineConfiguration) -> StageGraph:
    """Build the stage graph this invocation submits, waits for, and cancels."""
    return s3_full_run_plan(
        analysis_pbs=args.stages.analysis,
        blocksci_pbs=args.stages.blocksci,
        mappings_pbs=args.stages.mappings,
        blocksci_workflow=args.blocksci.workflow,
        blocksci_task=args.blocksci.task,
    )


@dataclass
class S3SubmissionTracker:
    """Prepare marker state and remember every job for rollback on failure."""

    args: PipelineConfiguration
    target: S3Target
    access: S3Access
    submitted_jobs: list[tuple[str, str]]
    jobs: dict[str, str] = field(default_factory=dict)

    def prepare_stage(self, stage: str) -> None:
        """Clear this stage's stale remote terminal markers before qsub."""
        if self.args.dry_run:
            print(f"[dry-run] Would clear stale .pbs/{stage}.done|failed markers")
            return
        clear_s3_stage_markers(
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
        persist_pbs_job_id(self.args.runs_path / self.target.run_id, stage, job_id)


@dataclass
class S3StageRunner:
    """Submit the PBS implementation selected by a declarative stage graph.

    ``StagePlan`` deliberately carries only logical identity, runner, and
    dependencies.  This adapter is the one place that translates a stage kind
    into the concrete PBS command and submission boundary, so planning never
    becomes a service locator with a callable embedded in each plan node.
    """

    args: PipelineConfiguration
    target: S3Target
    plan: StageGraph
    tracker: S3SubmissionTracker

    @property
    def task(self) -> str:
        return self.args.blocksci.task

    @property
    def mappings_pbs(self) -> bool:
        return self.args.stages.mappings

    @property
    def separate_combined_report(self) -> bool:
        return self.args.stages.blocksci and self.task == "detect"

    def submit_all(self) -> None:
        """Submit the selected graph nodes in the established qsub order."""
        for stage in self.plan.scheduled():
            self.submit(stage)

    def submit(self, stage: StagePlan) -> None:
        """Dispatch one logical stage through its concrete PBS adapter."""
        handler = {
            StageKind.BASELINE: self._submit_analysis,
            StageKind.MAPPINGS: self._submit_mappings,
            StageKind.BLOCKSCI_UPDATE: self._submit_update,
            StageKind.BLOCKSCI_PARSE: self._submit_parse,
            StageKind.BLOCKSCI_WORK: self._submit_blocksci_work,
            StageKind.REPORT: self._submit_report,
        }.get(stage.kind)
        if handler is None:
            raise PBSError(f"No PBS submission adapter for stage {stage.name}")
        handler(stage)

    def _submit(self, job: PBSJobSpec) -> None:
        self.tracker.submit(job.stage, lambda: submit_pbs_job(job, self.args.dry_run))

    def _submit_analysis(self, stage: StagePlan) -> None:
        resources = stage_pbs_resources(self.args, "analysis")
        script = render_coinjoin_analysis_s3_pbs(
            target=self.target,
            image=resolve_pbs_image(self.args, DEFAULT_COINJOIN_ANALYSIS_IMAGE, "pbs_coinjoin_analysis_image"),
            command=coinjoin_analysis_pbs_command("collect_docker"),
            **resources,
        )
        self._submit(PBSJobSpec(stage.name, script))

    def _submit_mappings(self, stage: StagePlan) -> None:
        resources = stage_pbs_resources(self.args, "mappings")
        script = render_mappings_s3_pbs(
            target=self.target,
            enumerator_image=resolve_pbs_image(
                self.args, DEFAULT_MAPPINGS_ENUMERATOR_IMAGE, "pbs_mappings_enumerator_image"
            ),
            sake_image=resolve_pbs_image(self.args, DEFAULT_SAKE_IMAGE, "pbs_sake_image"),
            mining_fee_rate=self.args.mappings.mining_fee_rate,
            coordination_fee_rate=self.args.mappings.coordination_fee_rate,
            max_decomposition_fee=self.args.mappings.max_decomposition_fee,
            mode=self.args.mappings.mode,
            timeout=self.args.mappings.timeout,
            retry_timeout=self.args.mappings.retry_timeout,
            sake_seed=self.args.mappings.sake_seed,
            **resources,
        )
        self._submit(PBSJobSpec(stage.name, script, self.plan.dependency_ids(stage.name, self.tracker.jobs)))

    def _blocksci_resources(self) -> tuple[str, PBSResources]:
        return (
            resolve_pbs_image(self.args, DEFAULT_BLOCKSCI_IMAGE, "pbs_blocksci_image"),
            stage_pbs_resources(self.args, "blocksci"),
        )

    def _submit_update(self, stage: StagePlan) -> None:
        image, resources = self._blocksci_resources()
        external_bitcoin = self.args.blocksci.external_bitcoin_datadir
        script = render_blocksci_update_s3_pbs(
            target=self.target,
            source_run_id=required(self.args.blocksci.cache_source_run_id, "--blocksci-cache-source-run-id"),
            image=image,
            command=blocksci_update_pbs_command(self.target.run_id),
            external_bitcoin_datadir=Path(external_bitcoin) if external_bitcoin else None,
            bitcoin_blocks_uri=self.args.blocksci.bitcoin_blocks_uri,
            external_network=required(self.args.blocksci.network, "--blocksci-network"),
            external_max_block=required(self.args.blocksci.max_block, "--blocksci-max-block"),
            **resources,
        )
        self._submit(PBSJobSpec(stage.name, script))

    def _submit_parse(self, stage: StagePlan) -> None:
        image, resources = self._blocksci_resources()
        external_bitcoin = self.args.blocksci.external_bitcoin_datadir
        bitcoin_blocks_uri = self.args.blocksci.bitcoin_blocks_uri
        external_index = self.args.blocksci.external_blocksci_dir
        command = blocksci_parse_pbs_command(self.target.run_id)
        if external_bitcoin or bitcoin_blocks_uri:
            command = blocksci_parse_pbs_command(
                self.target.run_id,
                coin_type=required(self.args.blocksci.network, "--blocksci-network"),
                disk_path="/mnt/data",
                max_block_expression=str(required(self.args.blocksci.max_block, "--blocksci-max-block") + 1),
            )
        script = render_blocksci_parse_s3_pbs(
            target=self.target,
            image=image,
            command=command,
            external_bitcoin_datadir=Path(external_bitcoin) if external_bitcoin else None,
            bitcoin_blocks_uri=bitcoin_blocks_uri,
            external_blocksci_dir=Path(external_index) if external_index else None,
            external_network=self.args.blocksci.network,
            external_max_block=self.args.blocksci.max_block,
            **resources,
        )
        self._submit(PBSJobSpec(stage.name, script))

    def _submit_blocksci_work(self, stage: StagePlan) -> None:
        image, resources = self._blocksci_resources()
        if stage.name == "blocksci":
            script = render_blocksci_s3_pbs(
                target=self.target,
                image=image,
                command=blocksci_pbs_command(
                    self.target.run_id, self.args, include_report=not self.separate_combined_report
                ),
                **resources,
                include_report=not self.separate_combined_report,
                export_analysis=self.separate_combined_report,
            )
            self._submit(PBSJobSpec(stage.name, script))
            return
        command = self._blocksci_work_command()
        script = render_blocksci_analyze_s3_pbs(
            target=self.target,
            image=image,
            command=command,
            mode=stage.name,
            user_script=Path(required(self.args.blocksci.script, "--blocksci-script"))
            if self.task == "script"
            else None,
            external_baseline_uri=self.args.blocksci.external_baseline_uri if self.task == "external" else None,
            notebooks_dir=Path(self.args.blocksci.notebooks_dir)
            if self.task == "notebook" and self.args.blocksci.notebooks_dir
            else None,
            notebook_port=self.args.blocksci.notebook_port or 8888,
            **resources,
        )
        self._submit(PBSJobSpec(stage.name, script, self.plan.dependency_ids(stage.name, self.tracker.jobs)))

    def _blocksci_work_command(self) -> str:
        if self.task == "detect":
            return blocksci_analysis_pbs_command(self.target.run_id, self.args)
        if self.task == "external":
            return blocksci_external_report_pbs_command(self.target.run_id, self.args)
        if self.task == "script":
            return blocksci_script_pbs_command(self.target.run_id, self.args)
        return blocksci_notebook_pbs_command(self.args.blocksci.notebook_port or 8888)

    def _submit_report(self, stage: StagePlan) -> None:
        dependency_job_ids = self.plan.dependency_ids(stage.name, self.tracker.jobs)
        if not self.args.dry_run and len(dependency_job_ids) != len(stage.dependencies):
            raise PBSError("Could not obtain analyzer job IDs for the unified report dependency")
        script = render_unified_report_s3_pbs(
            target=self.target,
            image=resolve_unified_report_pbs_image(self.args),
            command=blocksci_export_pbs_command(
                self.target.run_id,
                self.args,
                uploader_image=resolve_uploader_image(self.args),
                unified_report_image=unified_report_image_reference(self.args),
            ),
            ncpus=resolve_unified_report_pbs_resource(self.args, "ncpus", DEFAULT_UNIFIED_REPORT_NCPUS),
            mem=resolve_unified_report_pbs_resource(self.args, "mem", DEFAULT_UNIFIED_REPORT_MEM),
            scratch=resolve_unified_report_pbs_resource(self.args, "scratch", DEFAULT_UNIFIED_REPORT_SCRATCH),
            walltime=resolve_unified_report_pbs_resource(self.args, "walltime", DEFAULT_UNIFIED_REPORT_WALLTIME),
            include_mappings=self.mappings_pbs,
        )
        self._submit(PBSJobSpec(stage.name, script, dependency_job_ids))


def submit_s3_pbs_graph(args: PipelineConfiguration) -> S3PBSJobs:
    """Serialise one S3 PBS submission and roll it back on submit failure."""
    if not args.dry_run:
        run_id = required(args.run_id, "--run-id")
        acquire_lock(pbs_submit_lock_path(args.runs_path, run_id))
        ensure_no_active_s3_pbs_submission(args.runs_path / run_id)

    submitted_jobs: list[tuple[str, str]] = []
    try:
        return _submit_s3_pbs_stages(args, submitted_jobs)
    except BaseException:
        rollback_s3_pbs_submissions(submitted_jobs)
        raise


def _submit_s3_pbs_stages(args: PipelineConfiguration, submitted_jobs: list[tuple[str, str]]) -> S3PBSJobs:
    """Submit the concrete S3 PBS DAG while preserving marker semantics."""
    target = S3Target.from_args(args)
    access = target.access
    plan = s3_full_run_plan_from_args(args)
    tracker = S3SubmissionTracker(args, target, access, submitted_jobs)
    task = args.blocksci.task
    if task == "update" and not args.dry_run:
        source_run_id = args.blocksci.cache_source_run_id
        s3_access_preflight(access, target.artifact_uri)
        if not s3_object_exists(
            access,
            f"{target.artifact_uri}/{source_run_id}/{BLOCKSCI_PARSE_DIR}/manifest.json",
        ):
            raise ArtifactTransportError(f"source BlockSci cache manifest does not exist for run {source_run_id}")
        ensure_empty_run_prefix(access, target.artifact_uri, target.run_id)
    if (
        not args.dry_run
        and not args.stages.analysis
        and (args.stages.mappings or (args.stages.blocksci and task == "detect"))
    ):
        s3_access_preflight(access, target.artifact_uri)
        if not s3_object_exists(
            access,
            f"{target.artifact_uri}/{target.run_id}/{BASELINE_FILE}",
        ):
            raise ArtifactTransportError(
                f"resuming without --analysisPbs requires an existing {BASELINE_FILE} for run {target.run_id}"
            )
    if not args.dry_run and (args.stage_exporters or pbs_stages_need_exporters(args)):
        ensure_staged_exporters(args)
    S3StageRunner(args, target, plan, tracker).submit_all()
    return S3PBSJobs.from_plan(plan, tracker.jobs)
