"""PBS stage adapters for runs stored on the shared filesystem."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from client.pbs import (
    DEFAULT_BLOCKSCI_IMAGE,
    DEFAULT_BLOCKSCI_MEM,
    DEFAULT_BLOCKSCI_NCPUS,
    DEFAULT_BLOCKSCI_SCRATCH,
    DEFAULT_BLOCKSCI_WALLTIME,
    DEFAULT_COINJOIN_ANALYSIS_IMAGE,
    PBSError,
    blocksci_export_pbs_command,
    blocksci_pbs_command,
    coinjoin_analysis_pbs_command,
)
from client.pbs_settings import (
    pbs_wait_timeout,
    resolve_pbs_image,
    resolve_pbs_resource,
    stage_pbs_resources,
)


@dataclass(frozen=True)
class SharedStoragePBSOperations:
    """Wrapper-resolved I/O operations for shared-storage PBS stages."""

    compose_environment_from_args: Callable[..., Mapping[str, str]]
    compose_environment: Callable[..., Mapping[str, str]]
    stage_blocksci_script: Callable[[str | None, Path], str | None]
    stage_exporters: Callable[[Path, Path], Path]
    submit_blocksci: Callable[..., str | None]
    submit_analysis: Callable[..., str | None]
    submit_mappings: Callable[..., str | None]
    wait_for_marker: Callable[..., None]


def run_blocksci_stage(
    args: argparse.Namespace,
    run_dir: Path,
    operations: SharedStoragePBSOperations,
    *,
    wait: bool = True,
    include_report: bool = True,
) -> None:
    """Submit BlockSci through PBS, optionally returning before completion."""
    if not args.pbs_bitcoin_datadir:
        raise PBSError("--blocksciPbs requires --pbs-bitcoin-datadir or PBS_BITCOIN_DATADIR")
    env = operations.compose_environment_from_args(args, run_dir.name)
    image = resolve_pbs_image(args, DEFAULT_BLOCKSCI_IMAGE, "pbs_blocksci_image")
    staged_script = operations.stage_blocksci_script(
        getattr(args, "blocksci_script", None), run_dir
    )
    command = blocksci_pbs_command(
        run_id=run_dir.name,
        coinjoin_type=args.coinjoin_type,
        min_input_count=args.min_input_count,
        joinmarket_detector=args.joinmarket_detector,
        joinmarket_min_base_fee=args.joinmarket_min_base_fee,
        joinmarket_percentage_fee=args.joinmarket_percentage_fee,
        joinmarket_max_depth=args.joinmarket_max_depth,
        include_report=include_report,
        export_analysis=not include_report,
        blocksci_script=staged_script,
    )
    resources = stage_pbs_resources(args, "blocksci")
    exporters_dir = operations.stage_exporters(
        run_dir, Path(env["EXPORTERS_DIR"]).expanduser().resolve()
    )
    operations.submit_blocksci(
        run_dir=run_dir,
        logs_root=Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve(),
        bitcoin_datadir=Path(args.pbs_bitcoin_datadir).expanduser().resolve(),
        exporters_dir=exporters_dir,
        image=image,
        command=command,
        **resources,
        dry_run=args.dry_run,
    )
    if wait and not args.dry_run:
        operations.wait_for_marker(
            run_dir, "blocksci", timeout_seconds=pbs_wait_timeout(resources["walltime"])
        )


def run_coinjoin_analysis_stage(
    args: argparse.Namespace,
    run_dir: Path,
    operations: SharedStoragePBSOperations,
    *,
    wait: bool = True,
) -> None:
    """Submit coinjoin-analysis through PBS, optionally returning before completion."""
    analysis_action = getattr(args, "analysis_action", "collect_docker")
    baseline_path = run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json"
    if analysis_action == "analyze_only" and not baseline_path.is_file():
        raise PBSError(f"analyze_only requires an existing baseline: {baseline_path}")
    resources = stage_pbs_resources(args, "analysis")
    operations.submit_analysis(
        run_dir=run_dir,
        output_dir=run_dir / "coinjoin-analysis_data",
        input_data_dir=run_dir / "coinjoin_emulator_data" / "data",
        image=resolve_pbs_image(
            args, DEFAULT_COINJOIN_ANALYSIS_IMAGE, "pbs_coinjoin_analysis_image"
        ),
        command=coinjoin_analysis_pbs_command(analysis_action),
        **resources,
        dry_run=args.dry_run,
    )
    if wait and not args.dry_run:
        operations.wait_for_marker(
            run_dir,
            "coinjoin-analysis",
            timeout_seconds=pbs_wait_timeout(resources["walltime"]),
        )


def run_mappings_stage(
    args: argparse.Namespace,
    run_dir: Path,
    operations: SharedStoragePBSOperations,
    *,
    wait: bool = True,
) -> None:
    """Run both Wasabi mapping tools in one PBS allocation."""
    if args.engine != "wasabi" or args.coinjoin_type != "wasabi2":
        raise PBSError("CoinJoin mappings are supported only for Wasabi/wasabi2 runs")
    resources = stage_pbs_resources(args, "mappings")
    operations.submit_mappings(
        run_dir,
        args.pbs_mappings_enumerator_image,
        args.pbs_sake_image,
        mining_fee_rate=args.mapping_mining_fee_rate,
        coordination_fee_rate=args.mapping_coordination_fee_rate,
        max_decomposition_fee=args.mapping_max_decomposition_fee,
        mode=args.mapping_mode,
        timeout=args.mapping_timeout,
        retry_timeout=args.mapping_retry_timeout,
        sake_seed=args.sake_seed,
        **resources,
        dry_run=args.dry_run,
    )
    if wait and not args.dry_run:
        operations.wait_for_marker(
            run_dir,
            "coinjoin-mappings",
            timeout_seconds=pbs_wait_timeout(resources["walltime"]),
        )


def run_blocksci_export_stage(
    args: argparse.Namespace,
    run_dir: Path,
    operations: SharedStoragePBSOperations,
    *,
    wait: bool = True,
) -> None:
    """Submit the report-only PBS job after both analyzers have succeeded."""
    if not args.pbs_bitcoin_datadir:
        raise PBSError("--blocksciPbs requires --pbs-bitcoin-datadir or PBS_BITCOIN_DATADIR")
    env = operations.compose_environment(run_dir.name)
    walltime = resolve_pbs_resource(args, "pbs_walltime", DEFAULT_BLOCKSCI_WALLTIME)
    exporters_dir = operations.stage_exporters(
        run_dir, Path(env["EXPORTERS_DIR"]).expanduser().resolve()
    )
    operations.submit_blocksci(
        run_dir=run_dir,
        logs_root=Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve(),
        bitcoin_datadir=Path(args.pbs_bitcoin_datadir).expanduser().resolve(),
        exporters_dir=exporters_dir,
        image=resolve_pbs_image(args, DEFAULT_BLOCKSCI_IMAGE, "pbs_blocksci_image"),
        command=blocksci_export_pbs_command(
            run_id=run_dir.name,
            coinjoin_type=args.coinjoin_type,
            min_input_count=args.min_input_count,
            joinmarket_detector=args.joinmarket_detector,
            joinmarket_min_base_fee=args.joinmarket_min_base_fee,
            joinmarket_percentage_fee=args.joinmarket_percentage_fee,
            joinmarket_max_depth=args.joinmarket_max_depth,
        ),
        ncpus=resolve_pbs_resource(args, "pbs_ncpus", DEFAULT_BLOCKSCI_NCPUS),
        mem=resolve_pbs_resource(args, "pbs_mem", DEFAULT_BLOCKSCI_MEM),
        scratch=resolve_pbs_resource(args, "pbs_scratch", DEFAULT_BLOCKSCI_SCRATCH),
        walltime=walltime,
        dry_run=args.dry_run,
        stage="unified-report",
        job_name="blocksci_unified_report",
    )
    if wait and not args.dry_run:
        operations.wait_for_marker(
            run_dir, "unified-report", timeout_seconds=pbs_wait_timeout(walltime)
        )
