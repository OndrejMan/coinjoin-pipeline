"""Declarative stage plans for the CoinJoin analysis pipeline.

This module deliberately contains no Docker, Kubernetes, PBS, or subprocess
code.  It defines the dependency graph once; the wrapper remains responsible
for executing that graph with the existing, proven backend helpers.

Keeping planning separate from execution prevents every caller from having to
reconstruct the ``--analysisPbs`` / ``--blocksciPbs`` / ``--mappingsPbs``
matrix independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RunnerName = Literal["local", "kubernetes", "pbs"]


@dataclass(frozen=True)
class StagePlan:
    """One logical stage and the runner selected for it.

    ``dependencies`` refer to logical stage names, rather than job IDs.  The
    runner translates them into local ordering or scheduler dependencies.
    """

    name: str
    runner: RunnerName
    dependencies: tuple[str, ...] = ()
    produces_report: bool = False


@dataclass(frozen=True)
class AnalysisPlan:
    """The analysis/report portion of a run after emulation is available."""

    baseline: StagePlan
    blocksci: StagePlan
    mappings: StagePlan | None
    report: StagePlan | None
    parallel: bool

    def stages(self) -> tuple[StagePlan, ...]:
        """Return stages in deterministic submission/order-of-work order."""
        stages = [self.baseline]
        if self.mappings is not None:
            stages.append(self.mappings)
        stages.append(self.blocksci)
        if self.report is not None:
            stages.append(self.report)
        return tuple(stages)


def analysis_plan(
    *,
    analysis_pbs: bool,
    blocksci_pbs: bool,
    mappings_pbs: bool,
    parallel: bool,
) -> AnalysisPlan:
    """Build the shared-storage analysis DAG from the public execution flags.

    In serial mode the BlockSci stage retains the historical responsibility
    for report generation.  In parallel mode it exports compact BlockSci
    analysis data and a separate report stage joins both analyzer results.
    """
    baseline = StagePlan(
        name="coinjoin-analysis",
        runner="pbs" if analysis_pbs else "local",
    )
    mappings = (
        StagePlan(
            name="coinjoin-mappings",
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
        runner="pbs" if blocksci_pbs else "local",
        dependencies=blocksci_dependencies,
        produces_report=not parallel,
    )
    report = None
    if parallel:
        dependencies = [baseline.name, blocksci.name]
        if mappings is not None:
            dependencies.append(mappings.name)
        report = StagePlan(
            name="unified-report",
            runner="pbs" if blocksci_pbs else "local",
            dependencies=tuple(dependencies),
            produces_report=True,
        )
    return AnalysisPlan(
        baseline=baseline,
        blocksci=blocksci,
        mappings=mappings,
        report=report,
        parallel=parallel,
    )


def s3_full_run_plan(
    *,
    mappings_pbs: bool,
    blocksci_workflow: Literal["combined", "reusable"],
) -> tuple[StagePlan, ...]:
    """Build the canonical Kubernetes → S3 → PBS full-run graph.

    The two PBS analyzers are intentionally independent.  They publish their
    own artifacts, and only ``unified-report`` depends on both of them.  A
    reusable BlockSci workflow inserts a parser stage between emulation and
    BlockSci analysis.
    """
    emulation = StagePlan("kubernetes-emulation", "kubernetes")
    baseline = StagePlan("coinjoin-analysis", "pbs", (emulation.name,))
    mappings = (
        StagePlan("coinjoin-mappings", "pbs", (baseline.name,))
        if mappings_pbs
        else None
    )
    if blocksci_workflow == "reusable":
        blocksci_parse = StagePlan("blocksci-parse", "pbs", (emulation.name,))
        blocksci = StagePlan("blocksci-analyze", "pbs", (blocksci_parse.name,))
        stages = [emulation, baseline]
        if mappings is not None:
            stages.append(mappings)
        stages.extend((blocksci_parse, blocksci))
    else:
        # The combined worker downloads the emulation bundle from the same S3
        # prefix as the baseline worker, so it cannot start before the
        # Kubernetes uploader has published it either.
        blocksci = StagePlan("blocksci", "pbs", (emulation.name,))
        stages = [emulation, baseline]
        if mappings is not None:
            stages.append(mappings)
        stages.append(blocksci)
    report_dependencies = [baseline.name, blocksci.name]
    if mappings is not None:
        report_dependencies.append(mappings.name)
    stages.append(
        StagePlan(
            "unified-report",
            "pbs",
            tuple(report_dependencies),
            produces_report=True,
        )
    )
    return tuple(stages)
