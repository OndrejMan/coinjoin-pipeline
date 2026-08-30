"""CLI policy for the bare pipeline wrapper.

This module owns parsing, cross-option policy, command locking, and action
dispatch.  It deliberately receives concrete operations from ``wrapper``:
the wrapper remains the compatibility façade for existing callers and tests,
while this code has no dependency on that wide import surface.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from client.artifacts import ArtifactTransportError
from client.pbs import PBSError


@dataclass(frozen=True)
class WrapperOperations:
    """Concrete boundaries required to execute one parsed wrapper command.

    A mapping keeps the entrypoint independent of the legacy façade while
    allowing ``wrapper`` to bind its historical patch points at invocation
    time.  Tests and downstream callers can therefore retain their imports
    from ``client.wrapper`` during the transition.
    """

    values: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as error:
            raise AttributeError(name) from error


def _error_and_exit(error: Exception) -> None:
    print(f"[ERROR] {error}", file=sys.stderr)
    raise SystemExit(2)


def _validate_request(
    operations: WrapperOperations,
    parser: Any,
    args: Any,
    normalized_argv: list[str],
) -> None:
    """Apply public CLI, artifact, and cross-stage validation."""
    from coinjoin_pipeline.commands import action_from, validate_passthrough

    public_errors = validate_passthrough(normalized_argv, action_from(normalized_argv))
    if public_errors:
        parser.error("; ".join(public_errors))
    operations.validate_artifact_arguments(parser, args)
    if getattr(args, "blocksci_script", None):
        script_path = Path(args.blocksci_script).expanduser().resolve()
        if not script_path.is_file():
            parser.error(f"BlockSci script does not exist or is not a file: {script_path}")
        args.blocksci_script = str(script_path)
    if args.action == "clean" and not args.dry_run and not args.yes:
        parser.error("clean is destructive; pass --yes or use --dry-run")
    direct_kubernetes_pbs = (
        args.action == "full-run"
        and getattr(args, "driver", operations.default_driver) == "kubernetes"
        and getattr(args, "blocksciPbs", False)
        and not getattr(args, "copy_to_host", False)
    )
    if direct_kubernetes_pbs:
        kubernetes_datadir = getattr(args, "kubernetes_btc_datadir", None)
        pbs_datadir = getattr(args, "pbs_bitcoin_datadir", None)
        if (
            kubernetes_datadir
            and pbs_datadir
            and Path(kubernetes_datadir).expanduser().resolve()
            != Path(pbs_datadir).expanduser().resolve()
        ):
            parser.error(
                "direct Kubernetes storage requires --kubernetes-btc-datadir and "
                "--pbs-bitcoin-datadir to identify the same directory"
            )
    if getattr(args, "engine", None) == "joinmarket" and hasattr(args, "coinjoin_type"):
        if args.coinjoin_type == operations.default_coinjoin_type:
            args.coinjoin_type = "joinmarket"
    if getattr(args, "mappingsPbs", False) and getattr(args, "engine", None) != "wasabi":
        parser.error("--mappingsPbs is supported only with --engine wasabi")
    if getattr(args, "mappingsPbs", False) and args.action not in {
        "full-run", "mappings", "pbs-from-s3"
    }:
        parser.error("--mappingsPbs is supported only by full-run, mappings, and pbs-from-s3")
    if getattr(args, "mappingsPbs", False) and getattr(args, "coinjoin_type", None) != "wasabi2":
        parser.error("--mappingsPbs requires --coinjoin-type wasabi2")
    if args.action == "mappings" and not getattr(args, "mappingsPbs", False):
        parser.error("mappings requires --mappingsPbs")


def _print_dry_run(operations: WrapperOperations, args: Any) -> bool:
    """Print dry-run intent and return whether dispatch must continue."""
    use_pbs_dry_run = (
        (args.action == "analyze" and getattr(args, "blocksciPbs", False))
        or (args.action in ("coinjoin-analysis", "coinjoin") and getattr(args, "analysisPbs", False))
        or (args.action == "mappings" and getattr(args, "mappingsPbs", False))
        or args.action == "pbs-from-s3"
        or (
            args.action in ("emulate", "full-run")
            and getattr(args, "artifact_backend", "shared-storage") == "s3"
        )
    )
    if not args.dry_run:
        return True
    print(f"[dry-run] action: {args.action}")
    print(f"[dry-run] runtime: {args.runtime}")
    if hasattr(args, "engine"):
        print(f"[dry-run] engine: {args.engine}")
    if use_pbs_dry_run:
        if args.action == "emulate":
            print("[dry-run] Kubernetes resources will be rendered but not applied with kubectl.")
        elif args.action == "full-run":
            print("[dry-run] Kubernetes resources and PBS job scripts will be rendered but not submitted.")
        else:
            print("[dry-run] PBS job script will be rendered but not submitted with qsub.")
        return True
    print("[dry-run] No containers, files, reports, or Kubernetes resources will be created.")
    return False


def _prepare_execution(operations: WrapperOperations, args: Any) -> tuple[Path, bool]:
    """Acquire the command lock and derive the execution backend."""
    logs_root = Path(operations.compose_env().get("EMULATION_LOGS_DIR", ".")).expanduser().resolve()
    lock_path = operations.command_lock_path(args, logs_root)
    if not (args.action == "pbs-from-s3" and args.dry_run):
        try:
            operations.acquire_lock(lock_path)
        except RuntimeError as error:
            _error_and_exit(error)
    if args.action == "pbs-from-s3" and not args.dry_run:
        args.pbs_submission_dir = lock_path.parent
    elif (
        args.action == "full-run"
        and getattr(args, "artifact_backend", "shared-storage") == "s3"
        and not args.dry_run
    ):
        args.pbs_submission_dir = logs_root / args.run_id
    local_build = getattr(args, "coinjoin_infrastructure_local_build", False) or operations.truthy_env(
        "COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD"
    )
    if local_build:
        os.environ["COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD"] = "1"
    use_kubernetes = getattr(args, "driver", operations.default_driver) == "kubernetes"
    return logs_root, use_kubernetes


def _run_emulate(
    operations: WrapperOperations, args: Any, logs_root: Path, use_kubernetes: bool, local_build: bool
) -> None:
    if use_kubernetes and getattr(args, "artifact_backend", "shared-storage") == "s3":
        try:
            if not args.dry_run:
                operations.stage_kubernetes_s3_run(args, operations.s3_access_from_args(args))
            operations.run_kubernetes_s3_emulation(args)
        except (ArtifactTransportError, RuntimeError) as error:
            _error_and_exit(error)
        return
    if use_kubernetes:
        before = operations.run_dirs(logs_root)
        with operations.captured_pipeline_stage(logs_root, "Kubernetes emulation") as stage_log:
            operations.run_kubernetes_emulation(
                scenario=args.scenario, engine=args.engine, namespace=args.namespace,
                reuse_namespace=args.reuse_namespace, image_prefix=args.image_prefix,
                kubeconfig=args.kubeconfig, coinjoin_infrastructure_local_build=local_build,
                run_timezone_name=args.run_timezone, kubernetes_btc_datadir=args.kubernetes_btc_datadir,
                copy_to_host=args.copy_to_host,
            )
        active_run = operations.detect_active_run(logs_root, before)
        if active_run is not None:
            stage_log.relocate_to_run(active_run)
        else:
            stage_log.relocate(logs_root / "_failed")
            if operations.pipeline_run_id_env():
                _error_and_exit(RuntimeError("Emulator did not produce the expected run directory."))
        return
    with operations.captured_pipeline_stage(logs_root, "Docker emulation") as stage_log:
        env = operations.compose_env(engine=args.engine)
        emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
        before = operations.run_dirs(emulation_logs_dir)
        operations.run_script(
            operations.emulate_script,
            *(["--scenario", args.scenario] if args.scenario else []),
            engine=args.engine, run_timezone_name=args.run_timezone,
        )
        active_run = operations.detect_active_run(emulation_logs_dir, before)
        if active_run:
            print(f"Active run: {active_run.name}")
            stage_log.relocate_to_run(active_run)
        else:
            stage_log.relocate(logs_root / "_failed")
            if operations.pipeline_run_id_env():
                _error_and_exit(RuntimeError("Emulator did not produce the expected run directory."))


def _run_full_run(
    operations: WrapperOperations, args: Any, logs_root: Path, use_kubernetes: bool, local_build: bool
) -> None:
    env = operations.compose_env_from_args(args)
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    if getattr(args, "artifact_backend", "shared-storage") == "s3":
        try:
            operations.run_full_run_s3(args)
        except (PBSError, RuntimeError) as error:
            _error_and_exit(error)
        return
    with operations.captured_pipeline_stage(logs_root, "Clean containers and volumes", logs_root / "_maintenance"):
        operations.run_script(operations.delete_script)
    before = operations.run_dirs(emulation_logs_dir)
    if use_kubernetes:
        with operations.captured_pipeline_stage(logs_root, "Kubernetes emulation") as emulation_log:
            operations.run_kubernetes_emulation(
                scenario=args.scenario, engine=args.engine, namespace=args.namespace,
                reuse_namespace=args.reuse_namespace, image_prefix=args.image_prefix,
                kubeconfig=args.kubeconfig, coinjoin_infrastructure_local_build=local_build,
                run_timezone_name=args.run_timezone,
                kubernetes_btc_datadir=(args.kubernetes_btc_datadir or args.pbs_bitcoin_datadir),
                copy_to_host=args.copy_to_host,
                prepare_local_analysis=not getattr(args, "blocksciPbs", False),
            )
    else:
        with operations.captured_pipeline_stage(logs_root, "Docker emulation") as emulation_log:
            operations.run_script(
                operations.emulate_script,
                *(["--scenario", args.scenario] if args.scenario else []),
                engine=args.engine, run_timezone_name=args.run_timezone,
            )
    active_run = operations.detect_active_run(emulation_logs_dir, before)
    if active_run is None:
        emulation_log.relocate(logs_root / "_failed")
        _error_and_exit(RuntimeError("Emulator completed without creating a run directory."))
    print(f"Active run: {active_run.name}")
    emulation_log.relocate_to_run(active_run)
    try:
        if args.parallel:
            operations.run_parallel_analysis(args, active_run, logs_root)
        else:
            operations.run_serial_analysis(args, active_run, logs_root)
    except (PBSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        _error_and_exit(error)


def run_main(operations: WrapperOperations, argv: list[str] | None = None) -> None:
    """Execute the public wrapper CLI with its bound host operations."""
    operations.install_termination_handlers()
    parser = operations.build_parser()
    normalized_argv = operations.normalize_argv(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(normalized_argv)
    _validate_request(operations, parser, args, normalized_argv)
    os.environ[operations.container_runtime_env] = args.runtime
    if not _print_dry_run(operations, args):
        return
    logs_root, use_kubernetes = _prepare_execution(operations, args)
    local_build = getattr(args, "coinjoin_infrastructure_local_build", False) or operations.truthy_env(
        "COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD"
    )
    if args.action == "pbs-from-s3":
        try:
            operations.run_pbs_from_s3(args)
        except (ArtifactTransportError, OSError, PBSError, RuntimeError) as error:
            _error_and_exit(error)
    elif args.action == "emulate":
        _run_emulate(operations, args, logs_root, use_kubernetes, local_build)
    elif args.action == "clean":
        with operations.captured_pipeline_stage(logs_root, "Clean containers and volumes", logs_root / "_maintenance"):
            operations.run_script(operations.delete_script)
    elif args.action == "mappings":
        env = operations.compose_env(engine=args.engine)
        run_dir = Path(args.run_dir).expanduser()
        if not run_dir.is_absolute():
            run_dir = Path(env["EMULATION_LOGS_DIR"]) / run_dir
        try:
            with operations.captured_pipeline_stage(logs_root, "CoinJoin mappings (PBS)", run_dir.resolve()):
                operations.run_mappings_pbs_stage(args, run_dir.resolve())
        except PBSError as error:
            _error_and_exit(error)
    elif args.action == "analyze":
        env = operations.compose_env_from_args(args, include_scenario=False)
        active_run_id = operations.resolve_run_id(args.run_dir, env)
        if not active_run_id:
            _error_and_exit(RuntimeError("No grouped emulation run folder found."))
        run_dir = (Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve() / active_run_id).resolve()
        if getattr(args, "blocksciPbs", False):
            try:
                with operations.captured_pipeline_stage(logs_root, "BlockSci analysis (PBS)", run_dir):
                    operations.run_blocksci_pbs_stage(args, run_dir)
            except PBSError as error:
                _error_and_exit(error)
        else:
            try:
                staged_script = operations.stage_blocksci_script(args.blocksci_script, run_dir)
            except ValueError as error:
                parser.error(str(error))
            with operations.captured_pipeline_stage(logs_root, "BlockSci analysis", logs_root / active_run_id):
                operations.run_script(
                    operations.analysis_script, active_run_id=active_run_id, engine=args.engine,
                    coinjoin_type=args.coinjoin_type, min_input_count=args.min_input_count,
                    scenario=args.scenario, joinmarket_detector=args.joinmarket_detector,
                    joinmarket_min_base_fee=args.joinmarket_min_base_fee,
                    joinmarket_percentage_fee=args.joinmarket_percentage_fee,
                    joinmarket_max_depth=args.joinmarket_max_depth, blocksci_script=staged_script,
                )
    elif args.action == "export":
        active_run_id = operations.resolve_run_id(args.run_dir, operations.compose_env())
        if not active_run_id:
            _error_and_exit(RuntimeError("No grouped emulation run folder found."))
        with operations.captured_pipeline_stage(logs_root, "Unified report export", logs_root / active_run_id):
            operations.run_export_only(args)
    elif args.action in ("coinjoin-analysis", "coinjoin"):
        if getattr(args, "analysisPbs", False):
            env = operations.compose_env()
            active_run_id = operations.resolve_run_id(args.run_dir, env)
            if not active_run_id:
                _error_and_exit(RuntimeError("No grouped emulation run folder found."))
            run_dir = (Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve() / active_run_id).resolve()
            try:
                with operations.captured_pipeline_stage(logs_root, "coinjoin-analysis (PBS)", run_dir):
                    operations.run_coinjoin_analysis_pbs_stage(args, run_dir)
            except PBSError as error:
                _error_and_exit(error)
        else:
            operations.run_coinjoin_analysis(args.run_dir, args.all_runs, args.analysis_action)
    elif args.action == "initialize":
        with operations.captured_pipeline_stage(logs_root, "Initialize container images", logs_root / "_maintenance"):
            operations.initialize_images()
    elif args.action == "full-run":
        _run_full_run(operations, args, logs_root, use_kubernetes, local_build)
