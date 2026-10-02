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
from coinjoin_pipeline.execution.run_context import pipeline_run_id_env
from coinjoin_pipeline.execution.workflow import SharedStorageStageRunner, shared_storage_analysis_plan

from . import containers, kubernetes_launch, settings, shared_storage_pbs
from .stage_executor import execute_analysis


def _run_emulate(args: PipelineConfiguration, logs_root: Path, local_build: bool) -> None:
    if args.driver == "kubernetes":
        before = containers.run_dirs(logs_root)
        with captured_pipeline_stage(logs_root, "Kubernetes emulation") as stage_log:
            kubernetes_launch.run_kubernetes_emulation(
                args, local_build=local_build, btc_datadir=args.kubernetes.btc_datadir
            )
        active_run = containers.detect_active_run(logs_root, before)
        if active_run is not None:
            stage_log.relocate_to_run(active_run)
        else:
            stage_log.relocate(logs_root / "_failed")
            if pipeline_run_id_env():
                exit_with_error(RuntimeError("Emulator did not produce the expected run directory."))
        return
    with captured_pipeline_stage(logs_root, "Docker emulation") as stage_log:
        env = containers.compose_env(args)
        emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
        before = containers.run_dirs(emulation_logs_dir)
        containers.run_script(
            settings.EMULATE_SCRIPT,
            *(["--scenario", args.scenario] if args.scenario else []),
            config=args,
        )
        active_run = containers.detect_active_run(emulation_logs_dir, before)
        if active_run:
            print(f"Active run: {active_run.name}")
            stage_log.relocate_to_run(active_run)
        else:
            stage_log.relocate(logs_root / "_failed")
            if pipeline_run_id_env():
                exit_with_error(RuntimeError("Emulator did not produce the expected run directory."))


def _run_full_run(args: PipelineConfiguration, logs_root: Path, local_build: bool) -> None:
    env = containers.compose_env(args)
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    with captured_pipeline_stage(logs_root, "Clean containers and volumes", logs_root / "_maintenance"):
        containers.run_script(settings.DELETE_SCRIPT)
    before = containers.run_dirs(emulation_logs_dir)
    if args.driver == "kubernetes":
        with captured_pipeline_stage(logs_root, "Kubernetes emulation") as emulation_log:
            kubernetes_launch.run_kubernetes_emulation(
                args,
                local_build=local_build,
                btc_datadir=args.kubernetes.btc_datadir or args.pbs.bitcoin_datadir,
                prepare_local_analysis=not args.stages.blocksci,
            )
    else:
        with captured_pipeline_stage(logs_root, "Docker emulation") as emulation_log:
            containers.run_script(
                settings.EMULATE_SCRIPT,
                *(["--scenario", args.scenario] if args.scenario else []),
                config=args,
            )
    active_run = containers.detect_active_run(emulation_logs_dir, before)
    if active_run is None:
        emulation_log.relocate(logs_root / "_failed")
        exit_with_error(RuntimeError("Emulator completed without creating a run directory."))
    print(f"Active run: {active_run.name}")
    emulation_log.relocate_to_run(active_run)
    try:
        run_analysis(args, active_run, logs_root)
    except (PBSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        exit_with_error(error)


def _selected_run_dir(args: PipelineConfiguration, env: dict[str, str]) -> Path:
    active_run_id = containers.resolve_run_id(args.run_dir, env)
    if not active_run_id:
        exit_with_error(RuntimeError("No grouped emulation run folder found."))
    return (Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve() / active_run_id).resolve()


def _run_analyze(args: PipelineConfiguration, logs_root: Path) -> None:
    env = containers.compose_env(args, include_scenario=False)
    run_dir = _selected_run_dir(args, env)
    if args.stages.blocksci:
        try:
            with captured_pipeline_stage(logs_root, "BlockSci analysis (PBS)", run_dir):
                shared_storage_pbs.run_blocksci_stage(args, run_dir)
        except PBSError as error:
            exit_with_error(error)
        return
    staged_script = containers.stage_blocksci_script(args.blocksci.script, run_dir)
    with captured_pipeline_stage(logs_root, "BlockSci analysis", logs_root / run_dir.name):
        containers.run_script(
            settings.ANALYSIS_SCRIPT,
            config=args,
            active_run_id=run_dir.name,
            blocksci_script=staged_script,
        )


def run_configuration(args: PipelineConfiguration, logs_root: Path, local_build: bool) -> None:
    if args.action == "emulate":
        _run_emulate(args, logs_root, local_build)
    elif args.action == "full-run":
        _run_full_run(args, logs_root, local_build)
    elif args.action == "clean":
        with captured_pipeline_stage(logs_root, "Clean containers and volumes", logs_root / "_maintenance"):
            containers.run_script(settings.DELETE_SCRIPT)
    elif args.action == "mappings":
        env = containers.compose_env(args)
        run_dir = Path(required(args.run_dir, "--run-dir")).expanduser()
        if not run_dir.is_absolute():
            run_dir = Path(env["EMULATION_LOGS_DIR"]) / run_dir
        try:
            with captured_pipeline_stage(logs_root, "CoinJoin mappings (PBS)", run_dir.resolve()):
                shared_storage_pbs.run_mappings_stage(args, run_dir.resolve())
        except PBSError as error:
            exit_with_error(error)
    elif args.action == "analyze":
        _run_analyze(args, logs_root)
    elif args.action == "export":
        run_dir = _selected_run_dir(args, containers.compose_env(args))
        with captured_pipeline_stage(logs_root, "Unified report export", logs_root / run_dir.name):
            containers.run_export_only(args)
    elif args.action == "coinjoin-analysis":
        if args.stages.analysis:
            run_dir = _selected_run_dir(args, containers.compose_env(args))
            try:
                with captured_pipeline_stage(logs_root, "coinjoin-analysis (PBS)", run_dir):
                    shared_storage_pbs.run_coinjoin_analysis_stage(args, run_dir)
            except PBSError as error:
                exit_with_error(error)
        else:
            containers.run_coinjoin_analysis(args.run_dir, args.all_runs, args.analysis_action)
    elif args.action == "initialize":
        with captured_pipeline_stage(logs_root, "Initialize container images", logs_root / "_maintenance"):
            containers.initialize_images()


def run_analysis(args: PipelineConfiguration, run_dir: Path, logs_root: Path) -> None:
    graph = shared_storage_analysis_plan(args)
    runner = SharedStorageStageRunner(args, run_dir)
    name = "Parallel analysis" if args.parallel else "Serial analysis"
    with captured_pipeline_stage(logs_root, name, run_dir):
        execute_analysis(graph, runner, max_workers=max(1, len(graph)) if args.parallel else 1)
