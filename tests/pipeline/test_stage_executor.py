from __future__ import annotations

import threading
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

# A gate that a healthy test releases quickly; the timeout only stops a broken
# executor from hanging the suite.
GATE_TIMEOUT_SECONDS = 10


@dataclass
class RecordingRunner:
    """A runner that records its lifecycle and can be held or made to fail.

    ``log`` interleaves submissions and completions so a test can assert that
    a dependent stage was submitted only after its dependency finished, rather
    than inferring it from two separate orderings.
    """

    events: list[str] = field(default_factory=list)
    log: list[tuple[str, str]] = field(default_factory=list)
    submitted: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    failing_stage: str | None = None
    failing_submit: str | None = None
    gates: dict[str, threading.Event] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def hold(self, stage_name: str) -> None:
        """Block ``stage_name`` inside wait() until it is cancelled."""
        self.gates[stage_name] = threading.Event()

    def _record(self, event: str, stage_name: str) -> None:
        with self.lock:
            self.log.append((event, stage_name))

    def submit(self, stage: StagePlan) -> StageSubmission:
        with self.lock:
            self.submitted.append(stage.name)
        self._record("submit", stage.name)
        if stage.name == self.failing_submit:
            raise RuntimeError("submit refused")

        def wait() -> None:
            gate = self.gates.get(stage.name)
            if gate is not None:
                gate.wait(timeout=GATE_TIMEOUT_SECONDS)
            with self.lock:
                self.events.append(stage.name)
            self._record("wait", stage.name)
            if stage.name == self.failing_stage:
                raise RuntimeError("expected failure")

        def cancel() -> None:
            with self.lock:
                self.cancelled.append(stage.name)
            gate = self.gates.get(stage.name)
            if gate is not None:
                gate.set()

        return StageSubmission(stage, wait, cancel)


def _parallel_plan(*, mappings_pbs: bool = True):
    return analysis_plan(
        analysis_pbs=False,
        blocksci_pbs=False,
        mappings_pbs=mappings_pbs,
        parallel=True,
    )


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
    plan = _parallel_plan(mappings_pbs=False)
    runner = RecordingRunner(failing_stage="coinjoin-analysis")

    with pytest.raises(StageExecutionError, match="coinjoin-analysis"):
        execute_parallel_analysis(plan, runner)


def test_parallel_executor_leaves_the_report_to_the_caller() -> None:
    # The report joins both analyzers, but its stage logging and its
    # local-export-versus-PBS choice belong to the wrapper.
    plan = _parallel_plan()
    runner = RecordingRunner()

    execute_parallel_analysis(plan, runner)

    assert "unified-report" not in runner.submitted
    assert sorted(runner.events) == [
        "blocksci",
        "coinjoin-analysis",
        "coinjoin-mappings",
    ]


def test_parallel_executor_submits_mappings_only_after_the_baseline_finished() -> None:
    plan = _parallel_plan()
    runner = RecordingRunner()

    execute_parallel_analysis(plan, runner)

    assert runner.log.index(("submit", "coinjoin-mappings")) > runner.log.index(
        ("wait", "coinjoin-analysis")
    )
    # Both independent analyzers are submitted in the first pass, before any
    # join happens; only the dependent stage waits for its upstream.
    assert runner.log.index(("submit", "blocksci")) < runner.log.index(
        ("submit", "coinjoin-mappings")
    )


def test_parallel_executor_never_submits_a_stage_whose_dependency_failed() -> None:
    plan = _parallel_plan()
    runner = RecordingRunner(failing_stage="coinjoin-analysis")

    with pytest.raises(StageExecutionError, match="coinjoin-analysis"):
        execute_parallel_analysis(plan, runner)

    assert "coinjoin-mappings" not in runner.submitted
    # BlockSci does not depend on the baseline, so it still ran to completion.
    assert "blocksci" in runner.events


def test_parallel_executor_reports_a_refused_submission() -> None:
    plan = _parallel_plan()
    runner = RecordingRunner(failing_submit="blocksci")

    with pytest.raises(StageExecutionError, match="blocksci: submit refused"):
        execute_parallel_analysis(plan, runner)

    # A stage that could not be submitted must not stop the independent
    # baseline, nor the mappings stage that depends only on it.
    assert runner.events == ["coinjoin-analysis", "coinjoin-mappings"]


def test_parallel_executor_cancels_a_sibling_that_is_still_running() -> None:
    plan = _parallel_plan(mappings_pbs=False)
    runner = RecordingRunner(failing_stage="blocksci")
    runner.hold("coinjoin-analysis")

    with pytest.raises(StageExecutionError, match="blocksci"):
        execute_parallel_analysis(plan, runner)

    assert runner.cancelled == ["coinjoin-analysis"]


def test_parallel_executor_does_not_cancel_work_that_already_finished() -> None:
    plan = _parallel_plan()
    runner = RecordingRunner(failing_stage="coinjoin-mappings")
    # BlockSci is still running when mappings fails; the baseline has already
    # completed, which is what let mappings start at all.
    runner.hold("blocksci")

    with pytest.raises(StageExecutionError, match="coinjoin-mappings"):
        execute_parallel_analysis(plan, runner)

    assert runner.cancelled == ["blocksci"]
