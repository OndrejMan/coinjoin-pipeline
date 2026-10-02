"""Execute every stage in a dependency graph with bounded concurrency. Backend adapters own submission, waiting and cancellation."""

from __future__ import annotations

import concurrent.futures
import sys
from dataclasses import dataclass
from typing import Callable, Protocol

from coinjoin_pipeline.execution.stages import StageGraph, StagePlan


class StageExecutionError(RuntimeError):
    """One or more logical stages failed while executing a stage graph."""


@dataclass(frozen=True)
class StageSubmission:
    """A submitted stage and the operations available for its lifecycle."""

    stage: StagePlan
    wait: Callable[[], None]
    cancel: Callable[[], bool | None] | None = None


class StageRunner(Protocol):
    """Adapter implemented by a local, Kubernetes, or PBS execution backend."""

    def submit(self, stage: StagePlan) -> StageSubmission:
        """Submit or prepare ``stage`` and return its lifecycle handle."""


def _cancel_running(
    running: dict[concurrent.futures.Future[None], StageSubmission],
    failures: dict[str, Exception],
    cancelled: set[str],
) -> None:
    """Attempt every cancellation without masking the stage failure."""
    for future, submission in running.items():
        name = submission.stage.name
        if future.done() or submission.cancel is None or name in cancelled:
            continue
        cancelled.add(name)
        try:
            if submission.cancel() is False:
                raise RuntimeError("cancellation was not confirmed")
        except Exception as error:
            failures[f"{name} cancellation"] = error
            print(f"[pipeline] Could not cancel {name}: {error}", file=sys.stderr)


def execute_analysis(graph: StageGraph, runner: StageRunner, *, max_workers: int) -> None:
    """Execute ready stages, including report, with a bounded concurrency limit."""
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    pending = list(graph)
    completed: set[str] = set()
    failures: dict[str, Exception] = {}
    running: dict[concurrent.futures.Future[None], StageSubmission] = {}
    cancelled: set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        try:
            while pending or running:
                while pending and len(running) < max_workers and not failures:
                    stage = next(
                        (item for item in pending if set(item.dependencies) <= completed),
                        None,
                    )
                    if stage is None:
                        break
                    pending.remove(stage)
                    try:
                        submission = runner.submit(stage)
                    except Exception as error:
                        failures[stage.name] = error
                        pending.clear()
                        _cancel_running(running, failures, cancelled)
                        break
                    running[executor.submit(submission.wait)] = submission
                    print(f"[pipeline] stage {stage.name}: started")
                if not running:
                    if pending:
                        raise StageExecutionError("No stage is ready; dependencies were not satisfied")
                    break
                finished = next(concurrent.futures.as_completed(list(running)))
                submission = running.pop(finished)
                try:
                    finished.result()
                except Exception as error:
                    failures[submission.stage.name] = error
                    pending.clear()
                    _cancel_running(running, failures, cancelled)
                else:
                    completed.add(submission.stage.name)
                    print(f"[pipeline] stage {submission.stage.name}: done")
        except BaseException:
            _cancel_running(running, failures, cancelled)
            raise
    if failures:
        details = "; ".join(f"{stage}: {error}" for stage, error in failures.items())
        raise StageExecutionError(f"Analysis failed: {details}")
