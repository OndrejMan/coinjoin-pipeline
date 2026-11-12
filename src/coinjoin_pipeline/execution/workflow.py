"""EXPERIMENTAL: Execute local/shared-storage graph nodes through their owning adapters.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from coinjoin_pipeline.configuration import PipelineConfiguration

from . import containers
from .stage_executor import StageSubmission
from .stages import StageKind, StagePlan, analysis_plan


def shared_storage_analysis_plan(args: PipelineConfiguration):
    return analysis_plan()


class SharedStorageStageRunner:
    def __init__(self, args: PipelineConfiguration, run_dir: Path):
        self.args, self.run_dir = args, run_dir

    def submit(self, stage: StagePlan) -> StageSubmission:
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
