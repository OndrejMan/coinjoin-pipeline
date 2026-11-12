"""Submit one S3 PBS graph: the stage adapters, marker resets, job bookkeeping and rollback."""

from __future__ import annotations

from dataclasses import dataclass, field

from exporters.artifact_paths import BASELINE_FILE, BLOCKSCI_PARSE_DIR

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.locks import acquire_lock, ensure_no_active_s3_pbs_submission, pbs_submit_lock_path
from coinjoin_pipeline.execution.pbs.commands import (
    blocksci_analysis_pbs_command,
    blocksci_export_pbs_command,
    blocksci_notebook_pbs_command,
    blocksci_parse_pbs_command,
    blocksci_pbs_command,
    blocksci_script_pbs_command,
    coinjoin_analysis_pbs_command,
)
from coinjoin_pipeline.execution.pbs.submission import PBSJobSpec, persist_pbs_job_id, submit_pbs_job
from coinjoin_pipeline.execution.pbs.templates_s3 import (
    render_blocksci_analyze_s3_pbs,
    render_blocksci_parse_s3_pbs,
    render_blocksci_s3_pbs,
    render_coinjoin_analysis_s3_pbs,
    render_mappings_s3_pbs,
    render_unified_report_s3_pbs,
)
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pbs_settings import (
    pbs_image,
    resolve_unified_report_image,
    resolve_uploader_image,
    stage_pbs_resources,
    with_singularity_scheme,
)
from coinjoin_pipeline.execution.s3_markers import rollback_s3_pbs_submissions
from coinjoin_pipeline.execution.s3_staging import ensure_staged_exporters, pbs_stages_need_exporters
from coinjoin_pipeline.execution.stages import StageGraph, StageKind, StagePlan, runs_unified_report, s3_full_run_plan
from coinjoin_pipeline.registry import pinned_reference
from coinjoin_pipeline.storage.s3 import (
    ArtifactTransportError,
    S3Target,
    clear_s3_stage_markers,
    ensure_empty_run_prefix,
    run_prefix_uri,
    s3_access_preflight,
    s3_object_exists,
)


def s3_full_run_plan_from_args(args: PipelineConfiguration) -> StageGraph:
    """Build the stage graph this invocation submits, waits for, and cancels."""
    return s3_full_run_plan(
        analysis_pbs=args.stages.analysis,
        blocksci_pbs=args.stages.blocksci,
        mappings_pbs=args.stages.mappings,
        blocksci_workflow=args.blocksci.workflow,
        blocksci_task=args.blocksci.task,
        unified_report=runs_unified_report(
            action=args.action,
            analysis_pbs=args.stages.analysis,
            blocksci_pbs=args.stages.blocksci,
            blocksci_task=args.blocksci.task,
        ),
    )


@dataclass
class S3StageRunner:
    """Submit the PBS implementation of each stage of a declarative stage graph.

    ``StagePlan`` carries only logical identity, runner, and dependencies; this
    adapter is the one place that turns a stage kind into a rendered PBS job.
    Every job goes through ``_submit``, so none can skip the marker reset its
    wait depends on or the bookkeeping that rollback and dependencies read.
    """

    args: PipelineConfiguration
    target: S3Target
    plan: StageGraph
    submitted_jobs: list[tuple[str, str]]
    jobs: dict[str, str] = field(default_factory=dict)

    def submit_all(self) -> None:
        """Submit the selected graph nodes in the established qsub order."""
        handlers = {
            StageKind.BASELINE: self._analysis,
            StageKind.MAPPINGS: self._mappings,
            StageKind.BLOCKSCI_PARSE: self._parse,
            StageKind.BLOCKSCI_WORK: self._blocksci_work,
            StageKind.REPORT: self._report,
        }
        for stage in self.plan.scheduled():
            handlers[stage.kind](stage)

    def _submit(self, stage: StagePlan, script: str, *, depends: bool = False) -> None:
        dependencies = self.plan.dependency_ids(stage.name, self.jobs) if depends else ()
        if self.args.dry_run:
            print(f"[dry-run] Would clear stale .pbs/{stage.name}.done|failed markers")
        else:
            clear_s3_stage_markers(self.target.access, self.target.run_prefix, stage.name)
        job_id = submit_pbs_job(PBSJobSpec(stage.name, script, dependencies), self.args.dry_run)
        if job_id:
            self.submitted_jobs.append((stage.name, job_id))
            self.jobs[stage.name] = job_id
            persist_pbs_job_id(self.args.runs_path / self.target.run_id, stage.name, job_id)

    def _index(self) -> dict:
        """Renderer arguments of a job over a BlockSci index: its settings."""
        return {"blocksci": self.args.blocksci}

    def _analysis(self, stage: StagePlan) -> None:
        common: dict = {
            "target": self.target,
            "image": pbs_image(self.args, "coinjoin_analysis"),
            "resources": stage_pbs_resources(self.args, "analysis"),
        }
        script = render_coinjoin_analysis_s3_pbs(**common, command=coinjoin_analysis_pbs_command("collect_docker"))
        self._submit(stage, script)

    def _mappings(self, stage: StagePlan) -> None:
        script = render_mappings_s3_pbs(
            target=self.target,
            enumerator_image=pbs_image(self.args, "mappings_enumerator"),
            sake_image=pbs_image(self.args, "sake"),
            settings=self.args.mappings,
            resources=stage_pbs_resources(self.args, "mappings"),
        )
        self._submit(stage, script, depends=True)

    def _blocksci(self) -> dict:
        """Renderer arguments every BlockSci job shares."""
        return {
            "target": self.target,
            "image": pbs_image(self.args, "blocksci"),
            "resources": stage_pbs_resources(self.args, "blocksci"),
        }

    def _parse(self, stage: StagePlan) -> None:
        script = render_blocksci_parse_s3_pbs(
            **self._blocksci(), command=blocksci_parse_pbs_command(self.target.run_id)
        )
        self._submit(stage, script)

    def _blocksci_work(self, stage: StagePlan) -> None:
        blocksci, task = self.args.blocksci, self.args.blocksci.task
        if stage.name == "blocksci":
            # A detect run keeps the report in its own stage; any other combined run reports in place.
            report_here = not (self.args.stages.blocksci and task == "detect")
            script = render_blocksci_s3_pbs(
                **self._blocksci(),
                command=blocksci_pbs_command(self.target.run_id, self.args, include_report=report_here),
                include_report=report_here,
                export_analysis=not report_here,
            )
            self._submit(stage, script)
            return
        commands = {
            "detect": lambda: blocksci_analysis_pbs_command(self.target.run_id, self.args),
            "script": lambda: blocksci_script_pbs_command(self.target.run_id, self.args),
            "notebook": lambda: blocksci_notebook_pbs_command(blocksci.notebook_port),
        }
        script = render_blocksci_analyze_s3_pbs(
            **self._blocksci(), **self._index(), command=commands[task](), mode=stage.name
        )
        self._submit(stage, script, depends=True)

    def _report(self, stage: StagePlan) -> None:
        if not self.args.dry_run and len(self.plan.dependency_ids(stage.name, self.jobs)) != len(stage.dependencies):
            raise PBSError("Could not obtain analyzer job IDs for the unified report dependency")
        report_image = resolve_unified_report_image(self.args)
        script = render_unified_report_s3_pbs(
            target=self.target,
            image=with_singularity_scheme(report_image),
            command=blocksci_export_pbs_command(
                self.target.run_id,
                self.args,
                uploader_image=resolve_uploader_image(self.args),
                unified_report_image=pinned_reference(report_image),
            ),
            resources=stage_pbs_resources(self.args, "unified_report"),
            include_mappings=self.args.stages.mappings,
        )
        self._submit(stage, script, depends=True)


def submit_s3_pbs_graph(args: PipelineConfiguration) -> dict[str, str]:
    """Serialise one S3 PBS submission and roll it back on submit failure; return job IDs by stage."""
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


def _submit_s3_pbs_stages(args: PipelineConfiguration, submitted_jobs: list[tuple[str, str]]) -> dict[str, str]:
    """Submit the concrete S3 PBS DAG while preserving marker semantics."""
    target = S3Target.from_args(args)
    access = target.access
    plan = s3_full_run_plan_from_args(args)
    source_run_id = args.blocksci.cache_source_run_id
    # Update and read-only analyses both start a fresh run from another run's published cache.
    if source_run_id and not args.dry_run:
        s3_access_preflight(access, target.artifact_uri)
        if not s3_object_exists(
            access,
            f"{run_prefix_uri(target.artifact_uri, source_run_id)}/{BLOCKSCI_PARSE_DIR}/manifest.json",
        ):
            raise ArtifactTransportError(f"source BlockSci cache manifest does not exist for run {source_run_id}")
        ensure_empty_run_prefix(access, target.artifact_uri, target.run_id)
    reads_baseline = any(stage.kind in (StageKind.REPORT, StageKind.MAPPINGS) for stage in plan.stages)
    if not args.dry_run and not args.stages.analysis and reads_baseline:
        s3_access_preflight(access, target.artifact_uri)
        if not s3_object_exists(
            access,
            f"{run_prefix_uri(target.artifact_uri, target.run_id)}/{BASELINE_FILE}",
        ):
            raise ArtifactTransportError(
                f"resuming without --analysis-pbs requires an existing {BASELINE_FILE} for run {target.run_id}"
            )
    if not args.dry_run and (args.stage_exporters or pbs_stages_need_exporters(args)):
        ensure_staged_exporters(args)
    runner = S3StageRunner(args, target, plan, submitted_jobs)
    runner.submit_all()
    return runner.jobs
