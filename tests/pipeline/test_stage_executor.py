from __future__ import annotations

from dataclasses import dataclass, field
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from client.stage_executor import (
    StageExecutionError,
    StageSubmission,
    execute_parallel_analysis,
    execute_serial_analysis,
)
from client.stages import StagePlan, analysis_plan


@dataclass
class RecordingRunner:
    events: list[str] = field(default_factory=list)
    failing_stage: str | None = None

    def submit(self, stage: StagePlan) -> StageSubmission:
        def wait() -> None:
            self.events.append(stage.name)
            if stage.name == self.failing_stage:
                raise RuntimeError("expected failure")

        return StageSubmission(stage, wait)


def test_serial_executor_obeys_declared_dependencies() -> None:
    plan = analysis_plan(
        analysis_pbs=False,
        blocksci_pbs=False,
        mappings_pbs=True,
        parallel=False,
    )
    runner = RecordingRunner()

    execute_serial_analysis(plan, runner)

    assert runner.events == ["coinjoin-analysis", "coinjoin-mappings", "blocksci"]


def test_parallel_executor_does_not_export_after_a_failure() -> None:
    plan = analysis_plan(
        analysis_pbs=False,
        blocksci_pbs=False,
        mappings_pbs=False,
        parallel=True,
    )
    runner = RecordingRunner(failing_stage="coinjoin-analysis")

    with pytest.raises(StageExecutionError, match="coinjoin-analysis"):
        execute_parallel_analysis(plan, runner)
