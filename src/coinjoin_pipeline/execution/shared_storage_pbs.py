"""EXPERIMENTAL: PBS stage adapters for runs stored on the shared filesystem.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

from pathlib import Path

from exporters.artifact_paths import BASELINE_FILE, COINJOIN_ANALYSIS_DIR, EMULATOR_DIR

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.pbs.commands import (
    blocksci_export_pbs_command,
    blocksci_pbs_command,
    coinjoin_analysis_pbs_command,
)
from coinjoin_pipeline.execution.pbs.defaults import PBSResources
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pbs_settings import (
    pbs_image,
    pbs_wait_timeout,
    resolve_unified_report_image,
    stage_pbs_resources,
    with_singularity_scheme,
)

from . import containers
from .pbs.submission_local import (
    submit_blocksci_pbs,
    submit_coinjoin_analysis_pbs,
    submit_mappings_pbs,
    wait_for_pbs_marker,
)


def _wait_for_stage(args: PipelineConfiguration, run_dir: Path, stage: str, walltime: str, *, wait: bool) -> None:
    """Block on a stage marker unless the caller submits without joining.

    A dry run submits nothing, so there is never a marker to wait for.
    """
    if wait and not args.dry_run:
        wait_for_pbs_marker(run_dir, stage, timeout_seconds=pbs_wait_timeout(walltime))


def _submit_blocksci_job(
    args: PipelineConfiguration,
    run_dir: Path,
    stage: str,
    image: str,
    command: str,
    resources: PBSResources,
    *,
    wait: bool,
) -> None:
    """Submit one job in the BlockSci image over the shared run, Bitcoin datadir and exporters."""
    if not args.pbs.bitcoin_datadir:
        raise PBSError("--blocksciPbs requires --pbs-bitcoin-datadir or PBS_BITCOIN_DATADIR")
    env = containers.compose_env(args, run_dir.name)
    exporters_dir = Path(env["EXPORTERS_DIR"]).expanduser().resolve()
    if not args.dry_run:
        exporters_dir = containers.stage_pbs_exporters(run_dir, exporters_dir)
    submit_blocksci_pbs(
        run_dir=run_dir,
        logs_root=Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve(),
        bitcoin_datadir=Path(args.pbs.bitcoin_datadir).expanduser().resolve(),
        exporters_dir=exporters_dir,
        image=image,
        command=command,
        resources=resources,
        dry_run=args.dry_run,
        stage=stage,
    )
    _wait_for_stage(args, run_dir, stage, resources.walltime, wait=wait)


def run_blocksci_stage(
    args: PipelineConfiguration,
    run_dir: Path,
    *,
    wait: bool = True,
    include_report: bool = True,
) -> None:
    """Submit BlockSci through PBS, optionally returning before completion."""
    staged_script = (
        args.blocksci.script if args.dry_run else containers.stage_blocksci_script(args.blocksci.script, run_dir)
    )
    command = blocksci_pbs_command(run_dir.name, args, include_report=include_report, blocksci_script=staged_script)
    _submit_blocksci_job(
        args,
        run_dir,
        "blocksci",
        pbs_image(args, "blocksci"),
        command,
        stage_pbs_resources(args, "blocksci"),
        wait=wait,
    )


def run_blocksci_export_stage(args: PipelineConfiguration, run_dir: Path, *, wait: bool = True) -> None:
    """Submit the report-only PBS job after both analyzers have succeeded."""
    report_image = resolve_unified_report_image(args)
    _submit_blocksci_job(
        args,
        run_dir,
        "unified-report",
        with_singularity_scheme(report_image),
        blocksci_export_pbs_command(run_dir.name, args, unified_report_image=report_image),
        stage_pbs_resources(args, "unified_report"),
        wait=wait,
    )


def run_coinjoin_analysis_stage(args: PipelineConfiguration, run_dir: Path, *, wait: bool = True) -> None:
    """Submit coinjoin-analysis through PBS, optionally returning before completion."""
    baseline_path = run_dir / BASELINE_FILE
    if args.analysis_action == "analyze_only" and not baseline_path.is_file():
        raise PBSError(f"analyze_only requires an existing baseline: {baseline_path}")
    resources = stage_pbs_resources(args, "analysis")
    submit_coinjoin_analysis_pbs(
        run_dir=run_dir,
        output_dir=run_dir / COINJOIN_ANALYSIS_DIR,
        input_data_dir=run_dir / EMULATOR_DIR / "data",
        image=pbs_image(args, "coinjoin_analysis"),
        command=coinjoin_analysis_pbs_command(args.analysis_action),
        resources=resources,
        dry_run=args.dry_run,
    )
    _wait_for_stage(args, run_dir, "coinjoin-analysis", resources.walltime, wait=wait)


def run_mappings_stage(args: PipelineConfiguration, run_dir: Path, *, wait: bool = True) -> None:
    """Run both Wasabi mapping tools in one PBS allocation."""
    if args.engine != "wasabi" or args.coinjoin_type != "wasabi2":
        raise PBSError("CoinJoin mappings are supported only for Wasabi/wasabi2 runs")
    resources = stage_pbs_resources(args, "mappings")
    submit_mappings_pbs(
        run_dir,
        pbs_image(args, "mappings_enumerator"),
        pbs_image(args, "sake"),
        args.mappings,
        resources,
        dry_run=args.dry_run,
    )
    _wait_for_stage(args, run_dir, "coinjoin-mappings", resources.walltime, wait=wait)
