"""EXPERIMENTAL: Shared-storage PBS script renderers.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

from pathlib import Path

from coinjoin_pipeline.configuration import MappingsConfiguration
from coinjoin_pipeline.storage.s3 import shell_value

from .defaults import DEFAULT_RESOURCES, PBSResources
from .scripts import directives, mappings_run, template
from .validation import require_safe_image, require_safe_pbs_token, require_safe_template_path


def _render(name: str, stage: str, run_dir: Path, resources: PBSResources, **values: object) -> str:
    """Render one job whose markers and log live in the shared run directory."""
    require_safe_template_path(run_dir, "run directory")
    require_safe_pbs_token(stage, "PBS stage")
    preamble = directives(stage.replace("-", "_"), resources) + "\n" + template("local_preamble.sh")
    return template(name).format(
        preamble=preamble.format(run_dir=run_dir, stage=stage),
        run_dir=run_dir,
        stage=stage,
        **values,
    )


def render_blocksci_pbs(
    run_dir: Path,
    logs_root: Path,
    bitcoin_datadir: Path,
    exporters_dir: Path,
    image: str,
    command: str,
    resources: PBSResources = DEFAULT_RESOURCES["blocksci"],
    *,
    stage: str = "blocksci",
) -> str:
    """Render a PBS script for the BlockSci analysis or report stage."""
    for path, description in (
        (logs_root, "logs root"),
        (bitcoin_datadir, "Bitcoin datadir"),
        (exporters_dir, "exporters directory"),
    ):
        require_safe_template_path(path, description)
    require_safe_image(image)
    return _render(
        "local_blocksci.sh",
        stage,
        run_dir,
        resources,
        logs_root=logs_root,
        bitcoin_datadir=bitcoin_datadir,
        exporters_dir=exporters_dir,
        image=image,
        command=command,
    )


def render_coinjoin_analysis_pbs(
    run_dir: Path,
    output_dir: Path,
    input_data_dir: Path,
    image: str,
    command: str,
    resources: PBSResources = DEFAULT_RESOURCES["analysis"],
) -> str:
    """Render a PBS script for the coinjoin-analysis stage."""
    require_safe_template_path(output_dir, "output directory")
    require_safe_template_path(input_data_dir, "input data directory")
    require_safe_image(image)
    return _render(
        "local_coinjoin_analysis.sh",
        "coinjoin-analysis",
        run_dir,
        resources,
        output_dir=output_dir,
        input_data_dir=input_data_dir,
        image=image,
        command=command,
    )


def render_mappings_pbs(
    run_dir: Path,
    enumerator_image: str,
    sake_image: str,
    settings: MappingsConfiguration = MappingsConfiguration(),
    resources: PBSResources = DEFAULT_RESOURCES["mappings"],
) -> str:
    require_safe_image(enumerator_image, "enumerator image")
    require_safe_image(sake_image, "Sake image")
    return _render(
        "local_mappings.sh",
        "coinjoin-mappings",
        run_dir,
        resources,
        enumerator_image=shell_value(enumerator_image),
        sake_image=shell_value(sake_image),
        mappings_run=mappings_run(settings),
    )
