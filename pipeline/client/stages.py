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
    produces_report: bool = False


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

    def __iter__(self) -> Iterator[StagePlan]:
        return iter(self.stages)

    def __len__(self) -> int:
        return len(self.stages)

    def get(self, name: str) -> StagePlan | None:
        """Return the stage called ``name``, or ``None`` when absent."""
        for stage in self.stages:
            if stage.name == name:
                return stage
        return None

    def of_kind(self, kind: StageKind) -> StagePlan | None:
        """Return the single stage of ``kind``, or ``None`` when absent."""
        for stage in self.stages:
            if stage.kind is kind:
                return stage
        return None

    def scheduled(self) -> tuple[StagePlan, ...]:
        """Return the stages submitted to the batch scheduler, in wait order."""
        return tuple(stage for stage in self.stages if stage.runner == "pbs")

    def dependents_of(self, name: str) -> tuple[StagePlan, ...]:
        """Return every stage that transitively depends on ``name``.

        Used as the cancellation policy: when a stage fails, exactly its
        dependents can no longer run, while unrelated stages keep going and
        publish their own artifacts.
        """
        blocked = {name}
        dependents: list[StagePlan] = []
        for stage in self.stages:
            if stage.name in blocked:
                continue
            if blocked.intersection(stage.dependencies):
                blocked.add(stage.name)
                dependents.append(stage)
        return tuple(dependents)

    def dependency_ids(
        self, name: str, jobs: Mapping[str, str]
    ) -> tuple[str, ...]:
        """Return the submitted job IDs a stage must wait for, in graph order."""
        stage = self.get(name)
        if stage is None:
            return ()
        return tuple(
            jobs[dependency] for dependency in stage.dependencies if dependency in jobs
        )

    def dependency_id(self, name: str, jobs: Mapping[str, str]) -> str | None:
        """Return the single submitted job ID a stage depends on, if any."""
        job_ids = self.dependency_ids(name, jobs)
        if len(job_ids) > 1:
            raise ValueError(
                f"Stage {name} has {len(job_ids)} scheduler dependencies; "
                "use dependency_ids()"
            )
        return job_ids[0] if job_ids else None


def analysis_plan(
    *,
    analysis_pbs: bool,
    blocksci_pbs: bool,
    mappings_pbs: bool,
    parallel: bool,
) -> StageGraph:
    """Build the shared-storage analysis DAG from the public execution flags.

    In serial mode the BlockSci stage retains the historical responsibility
    for report generation, so the graph carries no report stage.  In parallel
    mode BlockSci exports compact analysis data and a separate report stage
    joins both analyzer results.
    """
    baseline = StagePlan(
        name="coinjoin-analysis",
        kind=StageKind.BASELINE,
        runner="pbs" if analysis_pbs else "local",
    )
    mappings = (
        StagePlan(
            name="coinjoin-mappings",
            kind=StageKind.MAPPINGS,
            runner="pbs",
            dependencies=(baseline.name,),
        )
        if mappings_pbs
        else None
    )
    blocksci_dependencies: tuple[str, ...] = ()
    if not parallel:
        blocksci_dependencies = (baseline.name,)
        if mappings is not None:
            blocksci_dependencies += (mappings.name,)
    blocksci = StagePlan(
        name="blocksci",
        kind=StageKind.BLOCKSCI_WORK,
        runner="pbs" if blocksci_pbs else "local",
        dependencies=blocksci_dependencies,
        produces_report=not parallel,
    )
    stages = [stage for stage in (baseline, mappings, blocksci) if stage is not None]
    if parallel:
        dependencies = [baseline.name, blocksci.name]
        if mappings is not None:
            dependencies.append(mappings.name)
        stages.append(
            StagePlan(
                name="unified-report",
                kind=StageKind.REPORT,
                runner="pbs" if blocksci_pbs else "local",
                dependencies=tuple(dependencies),
                produces_report=True,
            )
        )
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
    decoupled report exists only where a single BlockSci job cannot produce it
    on its own: when another analyzer contributes to it, or when the BlockSci
    work is split off from parsing.
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
    mappings = (
        StagePlan(
            name="coinjoin-mappings",
            kind=StageKind.MAPPINGS,
            runner="pbs",
            dependencies=((baseline or emulation).name,),
        )
        if mappings_pbs
        else None
    )
    parse, work = _blocksci_stages(
        emulation=emulation,
        blocksci_pbs=blocksci_pbs,
        blocksci_workflow=blocksci_workflow,
        blocksci_task=blocksci_task,
    )
    # Completion order: the analyzers publish independently, and mappings is
    # waited for after BlockSci because it is the shorter of the two tails.
    stages = [
        stage
        for stage in (emulation, baseline, parse, work, mappings)
        if stage is not None
    ]
    if _needs_decoupled_report(
        analysis_pbs=analysis_pbs,
        blocksci_pbs=blocksci_pbs,
        mappings_pbs=mappings_pbs,
        blocksci_workflow=blocksci_workflow,
        blocksci_task=blocksci_task,
    ):
        report_dependencies = [
            stage.name for stage in (baseline, work, mappings) if stage is not None
        ]
        stages.append(
            StagePlan(
                name="unified-report",
                kind=StageKind.REPORT,
                runner="pbs",
                dependencies=tuple(report_dependencies),
                produces_report=True,
            )
        )
    return StageGraph(tuple(stages))


def _needs_decoupled_report(
    *,
    analysis_pbs: bool,
    blocksci_pbs: bool,
    mappings_pbs: bool,
    blocksci_workflow: BlockSciWorkflow,
    blocksci_task: str,
) -> bool:
    """Report whether the unified report needs a joining stage of its own."""
    if blocksci_task != "detect" or not blocksci_pbs:
        return False
    return (
        analysis_pbs or mappings_pbs or blocksci_workflow != "combined"
    )


def combined_blocksci_exports_analysis(
    *,
    analysis_pbs: bool,
    blocksci_pbs: bool,
    mappings_pbs: bool,
    blocksci_task: str,
) -> bool:
    """Report whether the combined BlockSci job defers the report to a joiner.

    With another analyzer in the graph the combined job exports compact
    analysis data instead of writing the report itself, because the report
    must join results the job cannot see.
    """
    return (
        blocksci_pbs
        and blocksci_task == "detect"
        and (analysis_pbs or mappings_pbs)
    )
