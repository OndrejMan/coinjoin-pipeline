"""Declarative stage plans for the CoinJoin analysis pipeline.

No Docker, Kubernetes, PBS or subprocess code lives here: this is the dependency
graph, defined once. Submission derives an invocation's PBS jobs from it and
``--dry-run`` prints them.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Literal

RunnerName = Literal["local", "kubernetes", "pbs"]


class StageKind(Enum):
    """What kind of work a stage performs, independent of its stage name.

    Stage *names* are part of the marker and log protocol.  Runners and job
    bookkeeping dispatch on the kind instead, so renaming a marker cannot
    silently break a dispatch table.
    """

    EMULATION = "emulation"
    BASELINE = "baseline"
    BLOCKSCI_WORK = "blocksci-work"
    REPORT = "report"


@dataclass(frozen=True)
class StagePlan:
    """One logical stage and the runner selected for it.

    ``dependencies`` refer to logical stage names, rather than job IDs.  The
    runner translates them into local ordering.
    """

    name: str
    kind: StageKind
    runner: RunnerName
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageGraph:
    """An ordered stage DAG plus the queries its executors need.

    ``stages`` is ordered by completion: a stage never precedes one it
    depends on, and the order is the order in which a sequential orchestrator
    waits for the stages.
    """

    stages: tuple[StagePlan, ...]

    def __post_init__(self):
        self.ordered()

    def ordered(self) -> tuple[StagePlan, ...]:
        names = {stage.name for stage in self.stages}
        if len(names) != len(self.stages):
            raise ValueError("Stage names must be unique")
        if any(set(stage.dependencies) - names for stage in self.stages):
            raise ValueError("Stage dependency is absent from the graph")
        pending = list(self.stages)
        ordered = []
        completed: set[str] = set()
        while pending:
            ready = next(
                (stage for stage in pending if set(stage.dependencies) <= completed),
                None,
            )
            if ready is None:
                raise ValueError("Stage dependencies contain a cycle")
            pending.remove(ready)
            completed.add(ready.name)
            ordered.append(ready)
        return tuple(ordered)

    def __iter__(self) -> Iterator[StagePlan]:
        return iter(self.ordered())

    def __len__(self) -> int:
        return len(self.stages)

    def get(self, name: str) -> StagePlan | None:
        """Return the stage called ``name``, or ``None`` when absent."""
        for stage in self.stages:
            if stage.name == name:
                return stage
        return None

    def scheduled(self) -> tuple[StagePlan, ...]:
        """Return the stages submitted to the batch scheduler, in wait order."""
        return tuple(stage for stage in self.ordered() if stage.runner == "pbs")


def _report_stage(runner: RunnerName, *analyzers: StagePlan | None) -> StagePlan:
    """The unified report waits for every analyzer present in the graph."""
    return StagePlan(
        "unified-report",
        StageKind.REPORT,
        runner,
        tuple(stage.name for stage in analyzers if stage is not None),
    )


def analysis_plan() -> StageGraph:
    """Declare the experimental local graph: the same analyzer/report dependencies
    as ``s3_full_run_plan``, without an emulation node. Its node order is the
    serial execution order."""
    baseline = StagePlan("coinjoin-analysis", StageKind.BASELINE, "local")
    blocksci = StagePlan("blocksci", StageKind.BLOCKSCI_WORK, "local")
    stages = [baseline, blocksci]
    stages.append(_report_stage("local", baseline, blocksci))
    return StageGraph(tuple(stages))


def s3_full_run_plan(
    *,
    blocksci_pbs: bool = True,
) -> StageGraph:
    """Build the canonical Kubernetes → S3 → PBS graph for one invocation."""
    emulation = StagePlan(
        name="kubernetes-emulation",
        kind=StageKind.EMULATION,
        runner="kubernetes",
    )
    # The BlockSci worker downloads the emulation bundle from the run's S3 prefix,
    # so it cannot start before the Kubernetes uploader has published it.
    work = (
        StagePlan(
            name="blocksci",
            kind=StageKind.BLOCKSCI_WORK,
            runner="pbs",
            dependencies=(emulation.name,),
        )
        if blocksci_pbs
        else None
    )
    stages = [stage for stage in (emulation, work) if stage is not None]
    return StageGraph(tuple(stages))
