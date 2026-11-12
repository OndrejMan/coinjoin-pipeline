"""Inputs, outputs and checks of the work a job runs over an existing BlockSci index."""

from __future__ import annotations

from pathlib import Path

from exporters.artifact_paths import (
    ANALYSIS_ARTIFACT_FILE,
    BLOCKSCI_ANALYSIS_DIR,
    BLOCKSCI_CUSTOM_ANALYSIS_DIR,
    BLOCKSCI_NOTEBOOKS_DIR,
    EMULATOR_DIR,
    STAGED_EXPORTERS_DIR,
)

from coinjoin_pipeline.configuration import BlockSciConfiguration, required

from .stage_settings import AnalysisModeSettings, InputSettings, OutputSettings
from .validation import require_storage_path

# The run directory each mode writes: uploaded after the job, with the manifest of the index it read.
MODE_OUTPUTS = {
    "blocksci-analyze": BLOCKSCI_ANALYSIS_DIR,
    "blocksci-script": BLOCKSCI_CUSTOM_ANALYSIS_DIR,
    "blocksci-notebook": BLOCKSCI_NOTEBOOKS_DIR,
}


def analysis_mode_settings(mode: str, blocksci: BlockSciConfiguration) -> AnalysisModeSettings:
    """Settings of detection, a user script or a notebook (blocksci_analysis_jobs)."""
    output: OutputSettings = {"relative": MODE_OUTPUTS[mode]}
    inputs: list[InputSettings] = []
    settings: AnalysisModeSettings = {"output": output, "inputs": inputs, "output_check": None}
    if mode == "blocksci-analyze":
        inputs += [{"run": STAGED_EXPORTERS_DIR}, {"run": EMULATOR_DIR}]
        settings["output_check"] = [
            ANALYSIS_ARTIFACT_FILE,
            f"Reusable BlockSci analysis did not produce {ANALYSIS_ARTIFACT_FILE}",
        ]
    elif mode == "blocksci-script":
        # The configuration rules checked that the script exists; the node reads it from shared storage.
        script_path = Path(required(blocksci.script, "--blocksci-script")).expanduser().resolve()
        require_storage_path(script_path)
        settings["user_script"] = str(script_path)
    else:
        output["notebook"] = True
        if blocksci.notebooks_dir:
            notebook_path = Path(blocksci.notebooks_dir).expanduser().resolve()
            require_storage_path(notebook_path)
            # A submitting container's /storage is not the compute node's shared disk.
            # The worker validates existence there before starting Jupyter.
            output["notebooks_dir"] = str(notebook_path)
        settings["connection_help"] = [
            f"[blocksci-notebook] Preferred Jupyter port: {blocksci.notebook_port}",
            "[blocksci-notebook] Open JupyterLab from Scenario Lab; connection details follow after startup.",
        ]
    return settings
