"""EXPERIMENTAL: local Docker, Kubernetes without S3, and shared-storage PBS actions.

These paths serve local development and reproduction. The thesis results come
from the Kubernetes → S3 → PBS path dispatched in ``orchestrator.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pipeline_logging import captured_pipeline_stage, exit_with_error
from coinjoin_pipeline.execution.run_context import detect_active_run, run_dir_under_root
from coinjoin_pipeline.execution.workflow import SharedStorageStageRunner, shared_storage_analysis_plan

from . import containers, kubernetes_launch, settings, shared_storage_pbs
from .stage_executor import execute_analysis


def _emulate(args: PipelineConfiguration, logs_root: Path, local_build: bool, *, full_run: bool) -> Path:
    """Run the emulator on Docker or Kubernetes and file its log under the run it created."""
    on_kubernetes = args.driver == "kubernetes"
    with captured_pipeline_stage(logs_root, "Kubernetes emulation" if on_kubernetes else "Docker emulation") as log:
        if on_kubernetes:
            kubernetes_launch.run_kubernetes_emulation(
                args,
                local_build=local_build,
                btc_datadir=args.kubernetes.btc_datadir or (args.pbs.bitcoin_datadir if full_run else None),
                prepare_local_analysis=not (full_run and args.stages.blocksci),
            )
        else:
            containers.run_script(settings.EMULATE_SCRIPT, config=args)
    active_run = detect_active_run(logs_root)
    if active_run is None:
        log.relocate(logs_root / "_failed")
        exit_with_error(RuntimeError("Emulator did not produce the expected run directory."))
    print(f"Active run: {active_run.name}")
    log.relocate_to_run(active_run)
    return active_run


def _selected_run_dir(args: PipelineConfiguration, logs_root: Path) -> Path:
    return run_dir_under_root(required(args.run_dir, "run_dir"), logs_root)


def _on_pbs(logs_root: Path, stage_name: str, run_dir: Path, submit) -> None:
    try:
        with captured_pipeline_stage(logs_root, f"{stage_name} (PBS)", run_dir):
            submit()
    except PBSError as error:
        exit_with_error(error)


def run_configuration(args: PipelineConfiguration, logs_root: Path, local_build: bool) -> None:
    action = args.action
    if action in {"full-run", "clean"}:
        with captured_pipeline_stage(logs_root, "Clean containers and volumes", logs_root / "_maintenance"):
            containers.run_script(settings.DELETE_SCRIPT)
    if action == "emulate":
        _emulate(args, logs_root, local_build, full_run=False)
    elif action == "full-run":
        run_dir = _emulate(args, logs_root, local_build, full_run=True)
        try:
            run_analysis(args, run_dir, logs_root)
        except (PBSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
            exit_with_error(error)
    elif action == "analyze":
        run_dir = _selected_run_dir(args, logs_root)
        if args.stages.blocksci:
            _on_pbs(
                logs_root, "BlockSci analysis", run_dir, lambda: shared_storage_pbs.run_blocksci_stage(args, run_dir)
            )
            return
        staged_script = containers.stage_blocksci_script(args.blocksci.script, run_dir)
        with captured_pipeline_stage(logs_root, "BlockSci analysis", run_dir):
            containers.run_script(
                settings.ANALYSIS_SCRIPT, config=args, active_run_id=run_dir.name, blocksci_script=staged_script
            )
    elif action == "export":
        run_dir = _selected_run_dir(args, logs_root)
        with captured_pipeline_stage(logs_root, "Unified report export", run_dir):
            containers.run_export_only(args)
    elif action == "coinjoin-analysis":
        if args.stages.analysis:
            run_dir = _selected_run_dir(args, logs_root)
            _on_pbs(
                logs_root,
                "coinjoin-analysis",
                run_dir,
                lambda: shared_storage_pbs.run_coinjoin_analysis_stage(args, run_dir),
            )
        else:
            containers.run_coinjoin_analysis(args.run_dir, args.all_runs, args.analysis_action)
    elif action == "initialize":
        with captured_pipeline_stage(logs_root, "Initialize container images", logs_root / "_maintenance"):
            containers.initialize_images()


def run_analysis(args: PipelineConfiguration, run_dir: Path, logs_root: Path) -> None:
    graph = shared_storage_analysis_plan(args)
    runner = SharedStorageStageRunner(args, run_dir)
    name = "Parallel analysis" if args.parallel else "Serial analysis"
    with captured_pipeline_stage(logs_root, name, run_dir):
        execute_analysis(graph, runner, max_workers=max(1, len(graph)) if args.parallel else 1)
