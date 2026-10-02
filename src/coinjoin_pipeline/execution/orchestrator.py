"""Dispatch a resolved configuration.

The Kubernetes → S3 → PBS path (``pbs-from-s3`` and the S3 ``emulate`` and
``full-run``) is handled here. Every other action belongs to the experimental
local paths in ``local.py``.
"""

from __future__ import annotations

import os

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.locks import acquire_lock, command_lock_path
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pbs_settings import truthy_env
from coinjoin_pipeline.execution.pipeline_logging import exit_with_error
from coinjoin_pipeline.execution.s3_emulation import run_s3_kubernetes_emulation
from coinjoin_pipeline.execution.s3_staging import stage_kubernetes_s3_run
from coinjoin_pipeline.execution.s3_submission import submit_s3_pbs_graph
from coinjoin_pipeline.execution.s3_workflow import run_s3_full_run
from coinjoin_pipeline.storage.s3 import ArtifactTransportError

from . import local


def _print_dry_run(args: PipelineConfiguration) -> bool:
    """Print dry-run intent and return whether dispatch must continue."""
    use_pbs_dry_run = (
        args.action == "analyze"
        and args.stages.blocksci
        or (args.action == "coinjoin-analysis" and args.stages.analysis)
        or (args.action == "mappings" and args.stages.mappings)
        or (args.action == "pbs-from-s3")
        or (args.action in ("emulate", "full-run") and args.artifacts.backend == "s3")
    )
    if not args.dry_run:
        return True
    print(f"[dry-run] action: {args.action}")
    print(f"[dry-run] runtime: {args.runtime}")
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


def run_configuration(args: PipelineConfiguration) -> None:
    if not _print_dry_run(args):
        return
    logs_root = args.runs_path
    if not args.dry_run:
        try:
            acquire_lock(command_lock_path(args, logs_root))
        except RuntimeError as error:
            exit_with_error(error)
    local_build = args.kubernetes.infrastructure_local_build or truthy_env(
        "COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD"
    )
    if local_build:
        os.environ["COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD"] = "1"

    if args.action == "pbs-from-s3":
        try:
            submit_s3_pbs_graph(args)
        except (ArtifactTransportError, OSError, PBSError, RuntimeError) as error:
            exit_with_error(error)
    elif args.action == "emulate" and args.driver == "kubernetes" and args.artifacts.backend == "s3":
        try:
            if not args.dry_run:
                stage_kubernetes_s3_run(args)
            run_s3_kubernetes_emulation(args)
        except (ArtifactTransportError, RuntimeError) as error:
            exit_with_error(error)
    elif args.action == "full-run" and args.artifacts.backend == "s3":
        try:
            run_s3_full_run(args)
        except (PBSError, RuntimeError) as error:
            exit_with_error(error)
    else:
        local.run_configuration(args, logs_root, local_build)
