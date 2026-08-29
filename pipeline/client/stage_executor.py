"""Backend-neutral execution of declared analysis stages.

The executor owns dependency ordering and concurrent waiting.  Backend
adapters own the operational details: Compose commands, PBS submission,
marker polling, and cancellation.  This keeps the execution matrix out of the
top-level wrapper without hiding those operational contracts.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from typing import Callable, Protocol

from client.stages import AnalysisPlan, StagePlan


class StageExecutionError(RuntimeError):
    """One or more logical stages failed while executing an analysis plan."""


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


def execute_serial_analysis(plan: AnalysisPlan, runner: StageRunner) -> None:
    """Execute serial stages in their declared dependency order."""
    completed: set[str] = set()
    for stage in plan.stages():
        if stage is plan.report:
            continue
        missing = set(stage.dependencies) - completed
        if missing:
            raise StageExecutionError(
                f"Stage {stage.name} has unsatisfied dependencies: {', '.join(sorted(missing))}"
            )
        submission = runner.submit(stage)
        submission.wait()
        completed.add(stage.name)


def execute_parallel_analysis(plan: AnalysisPlan, runner: StageRunner) -> None:
    """Run independent analyzers concurrently, then schedule dependent mappings.

    The report stage is intentionally excluded: callers retain control over
    its stage logging and over whether it is a local export or a PBS job.
    """
    if not plan.parallel:
        raise ValueError("parallel executor requires an AnalysisPlan with parallel=True")

    failures: dict[str, Exception] = {}
    submissions: dict[concurrent.futures.Future[None], StageSubmission] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        baseline_future: concurrent.futures.Future[None] | None = None
        for stage in (plan.baseline, plan.blocksci):
            try:
                submission = runner.submit(stage)
            except Exception as error:
                failures[stage.name] = error
                continue
            future = executor.submit(submission.wait)
            submissions[future] = submission
            if stage.name == plan.baseline.name:
                baseline_future = future

        if plan.mappings is not None and baseline_future is not None:
            baseline_submission = submissions.pop(baseline_future)
            try:
                baseline_future.result()
            except Exception as error:
                failures[baseline_submission.stage.name] = error
            else:
                try:
                    mappings_submission = runner.submit(plan.mappings)
                except Exception as error:
                    failures[plan.mappings.name] = error
                else:
                    submissions[executor.submit(mappings_submission.wait)] = mappings_submission

        for future in concurrent.futures.as_completed(submissions):
            submission = submissions[future]
            try:
                future.result()
            except Exception as error:
                failures[submission.stage.name] = error
                for other_future, other_submission in submissions.items():
                    if other_future is not future and not other_future.done() and other_submission.cancel:
                        other_submission.cancel()

    if failures:
        details = "; ".join(
            f"{stage}: {error}" for stage, error in failures.items()
        )
        raise StageExecutionError(f"Parallel analysis failed: {details}")
