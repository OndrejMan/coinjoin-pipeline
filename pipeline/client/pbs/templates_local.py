"""Shared-storage PBS script renderers."""

from __future__ import annotations

from pathlib import Path

from .defaults import (
    DEFAULT_BLOCKSCI_MEM,
    DEFAULT_BLOCKSCI_NCPUS,
    DEFAULT_BLOCKSCI_SCRATCH,
    DEFAULT_BLOCKSCI_WALLTIME,
    DEFAULT_COINJOIN_ANALYSIS_MEM,
    DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
)
from .validation import (
    require_safe_image,
    require_safe_pbs_resources,
    require_safe_pbs_token,
    require_safe_template_path,
)


def render_blocksci_pbs(
    run_dir: Path,
    logs_root: Path,
    bitcoin_datadir: Path,
    exporters_dir: Path,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
    stage: str = "blocksci",
    job_name: str = "blocksci_analysis",
) -> str:
    """Render a PBS script for the BlockSci analysis stage."""
    for path, description in (
        (run_dir, "run directory"),
        (logs_root, "logs root"),
        (bitcoin_datadir, "Bitcoin datadir"),
        (exporters_dir, "exporters directory"),
    ):
        require_safe_template_path(path, description)
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    require_safe_pbs_token(stage, "PBS stage")
    require_safe_pbs_token(job_name, "PBS job name")
    template = (Path(__file__).parent.parent / "blocksci_template.sh").read_text(encoding="utf-8")
    return template.format(
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        run_dir=run_dir,
        logs_root=logs_root,
        bitcoin_datadir=bitcoin_datadir,
        exporters_dir=exporters_dir,
        image=image,
        blocksci_command=command,
        stage=stage,
        job_name=job_name,
    )


def render_coinjoin_analysis_pbs(
    run_dir: Path,
    output_dir: Path,
    input_data_dir: Path,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
) -> str:
    """Render a PBS script for the coinjoin-analysis stage."""
    for path, description in (
        (run_dir, "run directory"),
        (output_dir, "output directory"),
        (input_data_dir, "input data directory"),
    ):
        require_safe_template_path(path, description)
    require_safe_image(image)
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    template = (Path(__file__).parent.parent / "coinjoin_analysis_template.sh").read_text(encoding="utf-8")
    return template.format(
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        run_dir=run_dir,
        output_dir=output_dir,
        input_data_dir=input_data_dir,
        image=image,
        coinjoin_analysis_command=command,
    )


def render_mappings_pbs(
    run_dir: Path,
    enumerator_image: str,
    sake_image: str,
    *,
    mining_fee_rate: int = 1,
    coordination_fee_rate: float = 0.003,
    max_decomposition_fee: int = 6000,
    mode: str = "numeric",
    timeout: int = 60,
    retry_timeout: int = 600,
    sake_seed: int = 20260704,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
) -> str:
    require_safe_template_path(run_dir, "run directory")
    require_safe_image(enumerator_image, "enumerator image")
    require_safe_image(sake_image, "Sake image")
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    template = (Path(__file__).parent.parent / "mappings_template.sh").read_text(encoding="utf-8")
    return template.format(
        run_dir=run_dir,
        enumerator_image=enumerator_image,
        sake_image=sake_image,
        mining_fee_rate=mining_fee_rate,
        coordination_fee_rate=coordination_fee_rate,
        max_decomposition_fee=max_decomposition_fee,
        mode=mode,
        timeout=timeout,
        retry_timeout=retry_timeout,
        sake_seed=sake_seed,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
    )
