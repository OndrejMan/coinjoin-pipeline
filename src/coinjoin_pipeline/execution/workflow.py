"""EXPERIMENTAL: Execute local/shared-storage graph nodes through their owning adapters.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from coinjoin_pipeline.configuration import PipelineConfiguration

from . import containers, shared_storage_pbs
from .pbs.submission_local import qdel_pbs_stage, wait_for_pbs_marker
from .pbs_settings import pbs_wait_timeout, stage_pbs_walltime
from .stage_executor import StageSubmission
from .stages import StageKind, StagePlan, analysis_plan, resource_group


def shared_storage_analysis_plan(args: PipelineConfiguration):
    return analysis_plan(
        analysis_pbs=args.stages.analysis,
        blocksci_pbs=args.stages.blocksci,
        mappings_pbs=args.stages.mappings,
    )


class SharedStorageStageRunner:
    def __init__(self, args: PipelineConfiguration, run_dir: Path):
        self.args, self.run_dir = args, run_dir

    def submit(self, stage: StagePlan) -> StageSubmission:
        if stage.runner == "pbs":
            submitters: dict[StageKind, Callable[..., None]] = {
                StageKind.BASELINE: shared_storage_pbs.run_coinjoin_analysis_stage,
                StageKind.MAPPINGS: shared_storage_pbs.run_mappings_stage,
                StageKind.BLOCKSCI_WORK: shared_storage_pbs.run_blocksci_stage,
                StageKind.REPORT: shared_storage_pbs.run_blocksci_export_stage,
            }
            submit = submitters[stage.kind]
            options = {"include_report": False} if stage.kind is StageKind.BLOCKSCI_WORK else {}
            submit(self.args, self.run_dir, wait=False, **options)
            timeout = pbs_wait_timeout(stage_pbs_walltime(self.args, resource_group(stage.kind)))
            return StageSubmission(
                stage,
                wait=lambda: wait_for_pbs_marker(self.run_dir, stage.name, timeout_seconds=timeout),
                cancel=lambda: qdel_pbs_stage(self.run_dir, stage.name),
            )
        actions = {
            StageKind.BASELINE: lambda: containers.run_coinjoin_analysis_docker_stage(
                self.run_dir.name, self.args.analysis_action
            ),
            StageKind.BLOCKSCI_WORK: lambda: containers.run_blocksci_docker_stage(
                self.args, self.run_dir, include_report=False
            ),
            StageKind.REPORT: lambda: containers.run_export_only(replace(self.args, run_dir=str(self.run_dir))),
        }
        return StageSubmission(stage, wait=actions[stage.kind])
