"""Submit one S3 PBS graph: the stage adapters and their marker resets."""

from __future__ import annotations

from dataclasses import dataclass, field

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.pbs.commands import blocksci_pbs_command
from coinjoin_pipeline.execution.pbs.submission import PBSJobSpec, submit_pbs_job
from coinjoin_pipeline.execution.pbs.templates_s3 import render_blocksci_s3_pbs
from coinjoin_pipeline.execution.pbs_settings import (
    pbs_image,
    stage_pbs_resources,
)
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
    Every job goes through ``_submit``, so none can skip the reset of the stale
    markers a previous attempt left.
    """

    args: PipelineConfiguration
    target: S3Target
    plan: StageGraph
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
            self.jobs[stage.name] = job_id

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
    """Submit one S3 PBS graph; return job IDs by stage."""
    target = S3Target.from_args(args)
    plan = s3_full_run_plan_from_args(args)
    runner = S3StageRunner(args, target, plan)
    runner.submit_all()
    return runner.jobs
