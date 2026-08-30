"""Backend-neutral execution of a declared stage graph.

The executor owns dependency ordering and concurrent waiting.  Backend
adapters own the operational details: Compose commands, PBS submission,
marker polling, and cancellation.  This keeps the execution matrix out of the
top-level wrapper without hiding those operational contracts.

The executor knows no stage by name: it schedules whatever the graph declares
as ready.  The report stage is the one exception it recognises, and only to
leave it alone -- its stage logging and its local-export-versus-PBS choice
stay with the caller.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from typing import Callable, Protocol

from client.stages import StageGraph, StageKind, StagePlan


class StageExecutionError(RuntimeError):
    """One or more logical stages failed while executing a stage graph."""


@dataclass(frozen=True)
class StageSubmission:
    """A submitted stage and the operations available for its lifecycle."""

    stage: StagePlan
    wait: Callable[[], None]
    cancel: Callable[[], object] | None = None


class StageRunner(Protocol):
    """Adapter implemented by a local, Kubernetes, or PBS execution backend."""

    def submit(self, stage: StagePlan) -> StageSubmission:
        """Submit or prepare ``stage`` and return its lifecycle handle."""


def _executable_stages(graph: StageGraph) -> tuple[StagePlan, ...]:
    """Return the stages this executor runs, which is every non-report stage."""
    return tuple(stage for stage in graph if stage.kind is not StageKind.REPORT)


def execute_serial_analysis(graph: StageGraph, runner: StageRunner) -> None:
    """Execute stages one at a time in their declared dependency order."""
    completed: set[str] = set()
    for stage in _executable_stages(graph):
        missing = set(stage.dependencies) - completed
        if missing:
            raise StageExecutionError(
                f"Stage {stage.name} has unsatisfied dependencies: {', '.join(sorted(missing))}"
            )
        submission = runner.submit(stage)
        submission.wait()
        completed.add(stage.name)


def execute_parallel_analysis(graph: StageGraph, runner: StageRunner) -> None:
    """Run every stage as soon as the graph allows, and join them all.

    Cancellation here is deliberately broader than the S3 orchestrator's: a
    shared-storage run writes into one run directory and its report needs all
    analyzers, so a failure makes the whole join pointless and every sibling
    still running is cancelled.  Stages whose dependencies can no longer
    complete are simply never submitted.
    """
    stages = _executable_stages(graph)
    pending = list(stages)
    completed: set[str] = set()
    failures: dict[str, Exception] = {}
    running: dict[concurrent.futures.Future[None], StageSubmission] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(stages))) as executor:
        while True:
            ready = [
                stage for stage in pending if set(stage.dependencies) <= completed
            ]
            for stage in ready:
                pending.remove(stage)
                try:
                    submission = runner.submit(stage)
                except Exception as error:
                    failures[stage.name] = error
                    continue
                running[executor.submit(submission.wait)] = submission
            if not running:
                break

            finished = next(concurrent.futures.as_completed(list(running)))
            submission = running.pop(finished)
            try:
                finished.result()
            except Exception as error:
                failures[submission.stage.name] = error
                for other_future, other in running.items():
                    if not other_future.done() and other.cancel:
                        other.cancel()
            else:
                completed.add(submission.stage.name)

    if failures:
        details = "; ".join(f"{stage}: {error}" for stage, error in failures.items())
        raise StageExecutionError(f"Parallel analysis failed: {details}")
