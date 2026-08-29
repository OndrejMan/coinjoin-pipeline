"""Shared-storage stage adapters for the declared analysis graph.

This module maps logical stage names to the established Compose and PBS
helpers.  It intentionally receives those helpers as operations from the
wrapper: lower-level workflow code must not import the executable facade.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from client.stage_executor import StageSubmission
from client.stages import AnalysisPlan, StagePlan, analysis_plan


@dataclass(frozen=True)
class SharedStorageOperations:
    """Concrete Compose/PBS operations used by one shared-storage run."""

    run_coinjoin_analysis: Callable[..., None]
    run_coinjoin_analysis_docker: Callable[[str], None]
    run_coinjoin_analysis_pbs: Callable[..., None]
    run_mappings_pbs: Callable[..., None]
    run_blocksci_docker: Callable[..., None]
    run_blocksci_pbs: Callable[..., None]
    wait_for_pbs_marker: Callable[..., None]
    qdel_pbs_stage: Callable[[Path, str], bool]
    stage_wait_timeout: Callable[[argparse.Namespace, str], int]
    stage_blocksci_script: Callable[[str | None, Path], str | None]
    run_script: Callable[..., None]
    analysis_script: Path


def shared_storage_analysis_plan(
    args: argparse.Namespace,
    *,
    parallel: bool,
) -> AnalysisPlan:
    """Select shared-storage runners and dependencies from execution flags."""
    return analysis_plan(
        analysis_pbs=getattr(args, "analysisPbs", False),
        blocksci_pbs=getattr(args, "blocksciPbs", False),
        mappings_pbs=getattr(args, "mappingsPbs", False),
        parallel=parallel,
    )


class SharedStorageStageRunner:
    """Adapt established Compose/PBS helpers to the stage executor protocol."""

    def __init__(
        self,
        args: argparse.Namespace,
        run_dir: Path,
        *,
        parallel: bool,
        operations: SharedStorageOperations,
    ) -> None:
        self.args = args
        self.run_dir = run_dir
        self.parallel = parallel
        self.operations = operations

    def submit(self, stage: StagePlan) -> StageSubmission:
        if stage.name == "coinjoin-analysis":
            return self._submit_baseline(stage)
        if stage.name == "coinjoin-mappings":
            return self._submit_mappings(stage)
        if stage.name == "blocksci":
            return self._submit_blocksci(stage)
        raise ValueError(f"Unsupported shared-storage stage: {stage.name}")

    def _pbs_submission(
        self,
        stage: StagePlan,
        submit: Callable[[], None],
        timeout_seconds: int,
    ) -> StageSubmission:
        submit()
        return StageSubmission(
            stage,
            wait=lambda: self.operations.wait_for_pbs_marker(
                self.run_dir,
                stage.name,
                timeout_seconds=timeout_seconds,
            ),
            cancel=lambda: self.operations.qdel_pbs_stage(self.run_dir, stage.name),
        )

    def _submit_baseline(self, stage: StagePlan) -> StageSubmission:
        if stage.runner == "local":
            if self.parallel:
                return StageSubmission(
                    stage,
                    wait=lambda: self.operations.run_coinjoin_analysis_docker(self.run_dir.name),
                )
            return StageSubmission(
                stage,
                wait=lambda: self.operations.run_coinjoin_analysis(self.run_dir.name),
            )
        return self._pbs_submission(
            stage,
            lambda: self.operations.run_coinjoin_analysis_pbs(
                self.args, self.run_dir, wait=False
            ),
            self.operations.stage_wait_timeout(self.args, "analysis"),
        )

    def _submit_mappings(self, stage: StagePlan) -> StageSubmission:
        return self._pbs_submission(
            stage,
            lambda: self.operations.run_mappings_pbs(self.args, self.run_dir, wait=False),
            self.operations.stage_wait_timeout(self.args, "mappings"),
        )

    def _submit_blocksci(self, stage: StagePlan) -> StageSubmission:
        if stage.runner == "local":
            if not self.parallel:
                return StageSubmission(stage, wait=self._run_legacy_local_analysis)
            return StageSubmission(
                stage,
                wait=lambda: self.operations.run_blocksci_docker(
                    self.args,
                    self.run_dir,
                    include_report=stage.produces_report,
                ),
            )
        return self._pbs_submission(
            stage,
            lambda: self.operations.run_blocksci_pbs(
                self.args,
                self.run_dir,
                wait=False,
                include_report=stage.produces_report,
            ),
            self.operations.stage_wait_timeout(self.args, "blocksci"),
        )

    def _run_legacy_local_analysis(self) -> None:
        """Keep serial Compose behaviour byte-for-byte compatible for now."""
        staged_script = self.operations.stage_blocksci_script(
            self.args.blocksci_script, self.run_dir
        )
        self.operations.run_script(
            self.operations.analysis_script,
            active_run_id=self.run_dir.name,
            engine=self.args.engine,
            coinjoin_type=self.args.coinjoin_type,
            min_input_count=self.args.min_input_count,
            scenario=self.args.scenario,
            joinmarket_detector=self.args.joinmarket_detector,
            joinmarket_min_base_fee=self.args.joinmarket_min_base_fee,
            joinmarket_percentage_fee=self.args.joinmarket_percentage_fee,
            joinmarket_max_depth=self.args.joinmarket_max_depth,
            blocksci_script=staged_script,
        )
