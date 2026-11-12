"""Submit one S3 PBS graph: the stage adapters, marker resets, job bookkeeping and rollback."""

from __future__ import annotations

from dataclasses import dataclass, field

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.locks import acquire_lock, ensure_no_active_s3_pbs_submission, pbs_submit_lock_path
from coinjoin_pipeline.execution.pbs.commands import blocksci_pbs_command
from coinjoin_pipeline.execution.pbs.submission import PBSJobSpec, persist_pbs_job_id, submit_pbs_job
from coinjoin_pipeline.execution.pbs.templates_s3 import render_blocksci_s3_pbs
from coinjoin_pipeline.execution.pbs_settings import (
    pbs_image,
    stage_pbs_resources,
)
from coinjoin_pipeline.execution.s3_markers import rollback_s3_pbs_submissions
from coinjoin_pipeline.execution.s3_staging import ensure_staged_exporters, pbs_stages_need_exporters
from coinjoin_pipeline.execution.stages import StageGraph, StageKind, StagePlan, s3_full_run_plan
from coinjoin_pipeline.storage.s3 import (
    S3Target,
    clear_s3_stage_markers,
)


def s3_full_run_plan_from_args(args: PipelineConfiguration) -> StageGraph:
    """Build the stage graph this invocation submits, waits for, and cancels."""
    return s3_full_run_plan(
        blocksci_pbs=args.stages.blocksci,
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
            StageKind.BLOCKSCI_WORK: self._blocksci_work,
        }
        for stage in self.plan.scheduled():
            handlers[stage.kind](stage)

    def _submit(self, stage: StagePlan, script: str) -> None:
        if self.args.dry_run:
            print(f"[dry-run] Would clear stale .pbs/{stage.name}.done|failed markers")
        else:
            clear_s3_stage_markers(self.target.access, self.target.run_prefix, stage.name)
        job_id = submit_pbs_job(PBSJobSpec(stage.name, script), self.args.dry_run)
        if job_id:
            self.submitted_jobs.append((stage.name, job_id))
            self.jobs[stage.name] = job_id
            persist_pbs_job_id(self.args.runs_path / self.target.run_id, stage.name, job_id)

    def _blocksci(self) -> dict:
        """Renderer arguments every BlockSci job shares."""
        return {
            "target": self.target,
            "image": pbs_image(self.args, "blocksci"),
            "resources": stage_pbs_resources(self.args, "blocksci"),
        }

    def _blocksci_work(self, stage: StagePlan) -> None:
        # A BlockSci PBS stage keeps the report in its own stage; only a run without one reports in place.
        report_here = not self.args.stages.blocksci
        script = render_blocksci_s3_pbs(
            **self._blocksci(),
            command=blocksci_pbs_command(self.target.run_id, self.args, include_report=report_here),
            include_report=report_here,
            export_analysis=not report_here,
        )
        self._submit(stage, script)


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
    plan = s3_full_run_plan_from_args(args)
    if not args.dry_run and (args.stage_exporters or pbs_stages_need_exporters(args)):
        ensure_staged_exporters(args)
    runner = S3StageRunner(args, target, plan, submitted_jobs)
    runner.submit_all()
    return runner.jobs
