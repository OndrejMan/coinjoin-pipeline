"""EXPERIMENTAL: PBS stage adapters for runs stored on the shared filesystem.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

from pathlib import Path

from exporters.artifact_paths import BASELINE_FILE, COINJOIN_ANALYSIS_DIR, EMULATOR_DIR

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.pbs.commands import (
    blocksci_export_pbs_command,
    blocksci_pbs_command,
    coinjoin_analysis_pbs_command,
)
from coinjoin_pipeline.execution.pbs.defaults import (
    DEFAULT_BLOCKSCI_IMAGE,
    DEFAULT_COINJOIN_ANALYSIS_IMAGE,
    DEFAULT_UNIFIED_REPORT_MEM,
    DEFAULT_UNIFIED_REPORT_NCPUS,
    DEFAULT_UNIFIED_REPORT_SCRATCH,
    DEFAULT_UNIFIED_REPORT_WALLTIME,
)
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pbs_settings import (
    pbs_wait_timeout,
    resolve_pbs_image,
    resolve_unified_report_pbs_image,
    resolve_unified_report_pbs_resource,
    stage_pbs_resources,
    unified_report_image_reference,
)

from . import containers
from .pbs.submission_local import (
    submit_blocksci_pbs,
    submit_coinjoin_analysis_pbs,
    submit_mappings_pbs,
    wait_for_pbs_marker,
)


def _wait_for_stage(
    args: PipelineConfiguration,
    run_dir: Path,
    stage: str,
    walltime: str,
    *,
    wait: bool,
) -> None:
    """Block on a stage marker unless the caller submits without joining.

    A dry run submits nothing, so there is never a marker to wait for.
    """
    if wait and not args.dry_run:
        wait_for_pbs_marker(run_dir, stage, timeout_seconds=pbs_wait_timeout(walltime))


def run_blocksci_stage(
    args: PipelineConfiguration,
    run_dir: Path,
    *,
    wait: bool = True,
    include_report: bool = True,
) -> None:
    """Submit BlockSci through PBS, optionally returning before completion."""
    if not args.pbs.bitcoin_datadir:
        raise PBSError("--blocksciPbs requires --pbs-bitcoin-datadir or PBS_BITCOIN_DATADIR")
    env = containers.compose_env(args, run_dir.name)
    image = resolve_pbs_image(args, DEFAULT_BLOCKSCI_IMAGE, "pbs_blocksci_image")
    staged_script = (
        args.blocksci.script if args.dry_run else containers.stage_blocksci_script(args.blocksci.script, run_dir)
    )
    command = blocksci_pbs_command(run_dir.name, args, include_report=include_report, blocksci_script=staged_script)
    resources = stage_pbs_resources(args, "blocksci")
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
        **resources,
        dry_run=args.dry_run,
    )
    _wait_for_stage(args, run_dir, "blocksci", resources["walltime"], wait=wait)


def run_coinjoin_analysis_stage(
    args: PipelineConfiguration,
    run_dir: Path,
    *,
    wait: bool = True,
) -> None:
    """Submit coinjoin-analysis through PBS, optionally returning before completion."""
    analysis_action = args.analysis_action
    baseline_path = run_dir / BASELINE_FILE
    if analysis_action == "analyze_only" and not baseline_path.is_file():
        raise PBSError(f"analyze_only requires an existing baseline: {baseline_path}")
    resources = stage_pbs_resources(args, "analysis")
    submit_coinjoin_analysis_pbs(
        run_dir=run_dir,
        output_dir=run_dir / COINJOIN_ANALYSIS_DIR,
        input_data_dir=run_dir / EMULATOR_DIR / "data",
        image=resolve_pbs_image(args, DEFAULT_COINJOIN_ANALYSIS_IMAGE, "pbs_coinjoin_analysis_image"),
        command=coinjoin_analysis_pbs_command(analysis_action),
        **resources,
        dry_run=args.dry_run,
    )
    _wait_for_stage(args, run_dir, "coinjoin-analysis", resources["walltime"], wait=wait)


def run_mappings_stage(
    args: PipelineConfiguration,
    run_dir: Path,
    *,
    wait: bool = True,
) -> None:
    """Run both Wasabi mapping tools in one PBS allocation."""
    if args.engine != "wasabi" or args.coinjoin_type != "wasabi2":
        raise PBSError("CoinJoin mappings are supported only for Wasabi/wasabi2 runs")
    resources = stage_pbs_resources(args, "mappings")
    submit_mappings_pbs(
        run_dir,
        required(args.pbs.mappings_enumerator_image, "--pbs-mappings-enumerator-image"),
        required(args.pbs.sake_image, "--pbs-sake-image"),
        mining_fee_rate=args.mappings.mining_fee_rate,
        coordination_fee_rate=args.mappings.coordination_fee_rate,
        max_decomposition_fee=args.mappings.max_decomposition_fee,
        mode=args.mappings.mode,
        timeout=args.mappings.timeout,
        retry_timeout=args.mappings.retry_timeout,
        sake_seed=args.mappings.sake_seed,
        **resources,
        dry_run=args.dry_run,
    )
    _wait_for_stage(args, run_dir, "coinjoin-mappings", resources["walltime"], wait=wait)


def run_blocksci_export_stage(
    args: PipelineConfiguration,
    run_dir: Path,
    *,
    wait: bool = True,
) -> None:
    """Submit the report-only PBS job after both analyzers have succeeded."""
    if not args.pbs.bitcoin_datadir:
        raise PBSError("--blocksciPbs requires --pbs-bitcoin-datadir or PBS_BITCOIN_DATADIR")
    env = containers.compose_env(args, run_dir.name)
    walltime = resolve_unified_report_pbs_resource(args, "walltime", DEFAULT_UNIFIED_REPORT_WALLTIME)
    exporters_dir = Path(env["EXPORTERS_DIR"]).expanduser().resolve()
    if not args.dry_run:
        exporters_dir = containers.stage_pbs_exporters(run_dir, exporters_dir)
    submit_blocksci_pbs(
        run_dir=run_dir,
        logs_root=Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve(),
        bitcoin_datadir=Path(args.pbs.bitcoin_datadir).expanduser().resolve(),
        exporters_dir=exporters_dir,
        image=resolve_unified_report_pbs_image(args),
        command=blocksci_export_pbs_command(
            run_dir.name, args, unified_report_image=unified_report_image_reference(args)
        ),
        ncpus=resolve_unified_report_pbs_resource(args, "ncpus", DEFAULT_UNIFIED_REPORT_NCPUS),
        mem=resolve_unified_report_pbs_resource(args, "mem", DEFAULT_UNIFIED_REPORT_MEM),
        scratch=resolve_unified_report_pbs_resource(args, "scratch", DEFAULT_UNIFIED_REPORT_SCRATCH),
        walltime=walltime,
        dry_run=args.dry_run,
        stage="unified-report",
        job_name="blocksci_unified_report",
    )
    _wait_for_stage(args, run_dir, "unified-report", walltime, wait=wait)
