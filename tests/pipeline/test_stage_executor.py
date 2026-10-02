from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from coinjoin_pipeline.execution.stage_executor import (
    StageExecutionError,
    StageSubmission,
    execute_analysis,
)
from coinjoin_pipeline.execution.stages import (
    StageGraph,
    StageKind,
    StagePlan,
    analysis_plan,
)


def _execute_parallel(graph: StageGraph, runner) -> None:
    execute_analysis(graph, runner, max_workers=len(graph))


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
    failing_cancel: str | None = None
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
            if stage.name == self.failing_cancel:
                raise OSError("cancel refused")

        return StageSubmission(stage, wait, cancel)


def _parallel_plan(*, mappings_pbs: bool = True):
    return analysis_plan(
        analysis_pbs=False,
        blocksci_pbs=False,
        mappings_pbs=mappings_pbs,
    )


def test_serial_executor_obeys_declared_dependencies() -> None:
    plan = analysis_plan(
        analysis_pbs=False,
        blocksci_pbs=False,
        mappings_pbs=True,
    )
    runner = RecordingRunner()

    execute_analysis(plan, runner, max_workers=1)

    assert runner.events == [
        "coinjoin-analysis",
        "blocksci",
        "coinjoin-mappings",
        "unified-report",
    ]


def test_parallel_executor_does_not_export_after_a_failure() -> None:
    plan = _parallel_plan(mappings_pbs=False)
    runner = RecordingRunner(failing_stage="coinjoin-analysis")

    with pytest.raises(StageExecutionError, match="coinjoin-analysis"):
        _execute_parallel(plan, runner)


def test_parallel_executor_runs_report_after_its_dependencies() -> None:
    # The report joins both analyzers, but its stage logging and its
    # local-export-versus-PBS choice belong to the wrapper.
    plan = _parallel_plan()
    runner = RecordingRunner()

    _execute_parallel(plan, runner)

    assert runner.events[-1] == "unified-report"
    assert sorted(runner.events) == [
        "blocksci",
        "coinjoin-analysis",
        "coinjoin-mappings",
        "unified-report",
    ]


def test_executor_logs_each_stage_so_the_report_order_is_visible(capsys) -> None:
    # The local-docker and PBS e2e tests read these lines to prove the report
    # started only after both analyzers finished.
    _execute_parallel(_parallel_plan(mappings_pbs=False), RecordingRunner())

    lines = capsys.readouterr().out.splitlines()
    report = lines.index("[pipeline] stage unified-report: started")
    assert lines.index("[pipeline] stage coinjoin-analysis: done") < report
    assert lines.index("[pipeline] stage blocksci: done") < report
    assert lines[-1] == "[pipeline] stage unified-report: done"


def test_parallel_executor_submits_mappings_only_after_the_baseline_finished() -> None:
    plan = _parallel_plan()
    runner = RecordingRunner()

    _execute_parallel(plan, runner)

    assert runner.log.index(("submit", "coinjoin-mappings")) > runner.log.index(("wait", "coinjoin-analysis"))
    # Both independent analyzers are submitted in the first pass, before any
    # join happens; only the dependent stage waits for its upstream.
    assert runner.log.index(("submit", "blocksci")) < runner.log.index(("submit", "coinjoin-mappings"))


def test_parallel_executor_never_submits_a_stage_whose_dependency_failed() -> None:
    plan = _parallel_plan()
    runner = RecordingRunner(failing_stage="coinjoin-analysis")

    with pytest.raises(StageExecutionError, match="coinjoin-analysis"):
        _execute_parallel(plan, runner)

    assert "coinjoin-mappings" not in runner.submitted
    # BlockSci does not depend on the baseline, so it still ran to completion.
    assert "blocksci" in runner.events


def test_parallel_executor_reports_a_refused_submission() -> None:
    plan = _parallel_plan()
    runner = RecordingRunner(failing_submit="blocksci")

    with pytest.raises(StageExecutionError, match="blocksci: submit refused"):
        _execute_parallel(plan, runner)

    # A stage that could not be submitted must not stop the independent
    # baseline, nor the mappings stage that depends only on it.
    assert runner.events == ["coinjoin-analysis"]


def test_parallel_executor_cancels_a_sibling_that_is_still_running() -> None:
    plan = _parallel_plan(mappings_pbs=False)
    runner = RecordingRunner(failing_stage="blocksci")
    runner.hold("coinjoin-analysis")

    with pytest.raises(StageExecutionError, match="blocksci"):
        _execute_parallel(plan, runner)

    assert runner.cancelled == ["coinjoin-analysis"]


def test_parallel_executor_does_not_cancel_work_that_already_finished() -> None:
    plan = _parallel_plan()
    runner = RecordingRunner(failing_stage="coinjoin-mappings")
    # BlockSci is still running when mappings fails; the baseline has already
    # completed, which is what let mappings start at all.
    runner.hold("blocksci")

    with pytest.raises(StageExecutionError, match="coinjoin-mappings"):
        _execute_parallel(plan, runner)

    assert runner.cancelled == ["blocksci"]


def test_parallel_executor_does_not_submit_more_work_after_wait_failure() -> None:
    runner = RecordingRunner(failing_stage="blocksci")
    runner.hold("coinjoin-analysis")

    with pytest.raises(StageExecutionError, match="blocksci"):
        _execute_parallel(_parallel_plan(), runner)

    assert "coinjoin-mappings" not in runner.submitted


def test_parallel_executor_keeps_original_error_when_cancellation_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "GATE_TIMEOUT_SECONDS", 0.1)
    runner = RecordingRunner(failing_stage="blocksci", failing_cancel="coinjoin-analysis")
    runner.hold("coinjoin-analysis")
    runner.hold("coinjoin-mappings")
    plan = StageGraph(
        (
            StagePlan("coinjoin-analysis", StageKind.BASELINE, "pbs"),
            StagePlan("blocksci", StageKind.BLOCKSCI_WORK, "pbs"),
            StagePlan("coinjoin-mappings", StageKind.MAPPINGS, "pbs"),
        )
    )

    with pytest.raises(StageExecutionError, match="blocksci: expected failure") as error:
        _execute_parallel(plan, runner)

    assert "cancel refused" in str(error.value)
    assert runner.cancelled == ["coinjoin-analysis", "coinjoin-mappings"]


def test_parallel_executor_cancels_submitted_work_on_interrupt(monkeypatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "GATE_TIMEOUT_SECONDS", 0.1)
    runner = RecordingRunner()
    runner.hold("coinjoin-analysis")
    submit = runner.submit

    def interrupted_submit(stage: StagePlan) -> StageSubmission:
        if stage.name == "blocksci":
            raise KeyboardInterrupt
        return submit(stage)

    monkeypatch.setattr(runner, "submit", interrupted_submit)
    with pytest.raises(KeyboardInterrupt):
        _execute_parallel(_parallel_plan(), runner)

    assert runner.cancelled == ["coinjoin-analysis"]
