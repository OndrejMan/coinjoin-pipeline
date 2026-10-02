"""Declarative stage plans for the CoinJoin analysis pipeline.

This module deliberately contains no Docker, Kubernetes, PBS, or subprocess
code.  It defines the dependency graph once; the runners execute that graph
with the existing, proven backend helpers.

The graph is the single source of truth for a run: submission derives its
scheduler dependencies from it, the orchestrator derives its wait order and
its cancellation policy from it, and ``--dry-run`` prints it.  Adding a stage
therefore means editing one plan function, not four call sites.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal

RunnerName = Literal["local", "kubernetes", "pbs"]
BlockSciWorkflow = Literal["combined", "reusable", "cached"]


class StageKind(Enum):
    """What kind of work a stage performs, independent of its stage name.

    Stage *names* are part of the marker and log protocol and vary with the
    selected BlockSci workflow and task (``blocksci`` versus
    ``blocksci-analyze`` versus ``blocksci-script``).  Runners and job
    bookkeeping dispatch on the kind instead, so renaming a marker cannot
    silently break a dispatch table.
    """

    EMULATION = "emulation"
    BASELINE = "baseline"
    MAPPINGS = "mappings"
    BLOCKSCI_PARSE = "blocksci-parse"
    BLOCKSCI_UPDATE = "blocksci-update"
    BLOCKSCI_WORK = "blocksci-work"
    REPORT = "report"


#: Several stage names share one PBS budget: every BlockSci job is budgeted
#: as ``blocksci`` whether it parses, analyzes, or runs a notebook.
RESOURCE_GROUPS: dict[StageKind, str] = {
    StageKind.BASELINE: "analysis",
    StageKind.MAPPINGS: "mappings",
    StageKind.BLOCKSCI_PARSE: "blocksci",
    StageKind.BLOCKSCI_UPDATE: "blocksci",
    StageKind.BLOCKSCI_WORK: "blocksci",
    StageKind.REPORT: "report",
}


@dataclass(frozen=True)
class StagePlan:
    """One logical stage and the runner selected for it.

    ``dependencies`` refer to logical stage names, rather than job IDs.  The
    runner translates them into local ordering or scheduler dependencies.
    """

    name: str
    kind: StageKind
    runner: RunnerName
    dependencies: tuple[str, ...] = ()


def resource_group(kind: StageKind) -> str:
    """Return the PBS resource/walltime group a stage kind is budgeted from."""
    return RESOURCE_GROUPS[kind]


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

    def dependents_of(self, name: str) -> tuple[StagePlan, ...]:
        """Return every stage that transitively depends on ``name``.

        Used as the cancellation policy: when a stage fails, exactly its
        dependents can no longer run, while unrelated stages keep going and
        publish their own artifacts.
        """
        blocked = {name}
        dependents: list[StagePlan] = []
        for stage in self.ordered():
            if stage.name in blocked:
                continue
            if blocked.intersection(stage.dependencies):
                blocked.add(stage.name)
                dependents.append(stage)
        return tuple(dependents)

    def dependency_ids(self, name: str, jobs: Mapping[str, str]) -> tuple[str, ...]:
        """Return the submitted job IDs a stage must wait for, in graph order."""
        stage = self.get(name)
        if stage is None:
            return ()
        return tuple(jobs[dependency] for dependency in stage.dependencies if dependency in jobs)

    def dependency_id(self, name: str, jobs: Mapping[str, str]) -> str | None:
        """Return the single submitted job ID a stage depends on, if any."""
        job_ids = self.dependency_ids(name, jobs)
        if len(job_ids) > 1:
            raise ValueError(f"Stage {name} has {len(job_ids)} scheduler dependencies; use dependency_ids()")
        return job_ids[0] if job_ids else None


def _mappings_stage(after: StagePlan) -> StagePlan:
    """Mappings read the baseline output, or the emulation upload when no baseline runs."""
    return StagePlan("coinjoin-mappings", StageKind.MAPPINGS, "pbs", (after.name,))


def _report_stage(runner: RunnerName, *analyzers: StagePlan | None) -> StagePlan:
    """The unified report waits for every analyzer present in the graph."""
    return StagePlan(
        "unified-report",
        StageKind.REPORT,
        runner,
        tuple(stage.name for stage in analyzers if stage is not None),
    )


def analysis_plan(*, analysis_pbs: bool, blocksci_pbs: bool, mappings_pbs: bool) -> StageGraph:
    """Declare the experimental local graph: the same analyzer/report dependencies
    as ``s3_full_run_plan``, without an emulation node. Its node order is the
    serial execution order."""
    baseline = StagePlan("coinjoin-analysis", StageKind.BASELINE, "pbs" if analysis_pbs else "local")
    blocksci = StagePlan("blocksci", StageKind.BLOCKSCI_WORK, "pbs" if blocksci_pbs else "local")
    mappings = _mappings_stage(baseline) if mappings_pbs else None
    stages = [stage for stage in (baseline, blocksci, mappings) if stage is not None]
    stages.append(_report_stage("pbs" if blocksci_pbs else "local", baseline, blocksci, mappings))
    return StageGraph(tuple(stages))


def _blocksci_stages(
    *,
    emulation: StagePlan,
    blocksci_pbs: bool,
    blocksci_workflow: BlockSciWorkflow,
    blocksci_task: str,
) -> tuple[StagePlan | None, StagePlan | None]:
    """Return the ``(parse, work)`` BlockSci stages for one S3 invocation.

    A reusable workflow publishes a cache in its own stage; a cached workflow
    consumes one published earlier, so its work stage only waits for the
    emulation upload.  ``combined`` keeps parsing and analysis in one job.
    """
    if not blocksci_pbs:
        return None, None
    if blocksci_task == "update":
        return None, StagePlan(
            name="blocksci-update",
            kind=StageKind.BLOCKSCI_UPDATE,
            runner="pbs",
            dependencies=(emulation.name,),
        )
    if blocksci_workflow == "combined":
        # The combined worker downloads the emulation bundle from the same S3
        # prefix as the baseline worker, so it cannot start before the
        # Kubernetes uploader has published it either.
        return None, StagePlan(
            name="blocksci",
            kind=StageKind.BLOCKSCI_WORK,
            runner="pbs",
            dependencies=(emulation.name,),
        )
    parse = (
        StagePlan(
            name="blocksci-parse",
            kind=StageKind.BLOCKSCI_PARSE,
            runner="pbs",
            dependencies=(emulation.name,),
        )
        if blocksci_workflow == "reusable"
        else None
    )
    if blocksci_task == "parse":
        return parse, None
    work = StagePlan(
        name=f"blocksci-{'analyze' if blocksci_task == 'detect' else blocksci_task}",
        kind=StageKind.BLOCKSCI_WORK,
        runner="pbs",
        dependencies=((parse or emulation).name,),
    )
    return parse, work


def s3_full_run_plan(
    *,
    mappings_pbs: bool,
    blocksci_workflow: BlockSciWorkflow = "combined",
    analysis_pbs: bool = True,
    blocksci_pbs: bool = True,
    blocksci_task: str = "detect",
) -> StageGraph:
    """Build the canonical Kubernetes → S3 → PBS graph for one invocation.

    The two PBS analyzers are intentionally independent.  They publish their
    own artifacts, and only ``unified-report`` depends on both of them.  The
    report is a separate stage for every detect workflow, including resumes
    that consume a previously stored baseline.
    """
    emulation = StagePlan(
        name="kubernetes-emulation",
        kind=StageKind.EMULATION,
        runner="kubernetes",
    )
    baseline = (
        StagePlan(
            name="coinjoin-analysis",
            kind=StageKind.BASELINE,
            runner="pbs",
            dependencies=(emulation.name,),
        )
        if analysis_pbs
        else None
    )
    mappings = _mappings_stage(baseline or emulation) if mappings_pbs else None
    parse, work = _blocksci_stages(
        emulation=emulation,
        blocksci_pbs=blocksci_pbs,
        blocksci_workflow=blocksci_workflow,
        blocksci_task=blocksci_task,
    )
    # Completion order: the analyzers publish independently, and mappings is
    # waited for after BlockSci because it is the shorter of the two tails.
    stages = [stage for stage in (emulation, baseline, parse, work, mappings) if stage is not None]
    if blocksci_pbs and blocksci_task == "detect":
        stages.append(_report_stage("pbs", baseline, work, mappings))
    return StageGraph(tuple(stages))
