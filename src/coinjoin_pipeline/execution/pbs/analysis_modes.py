"""Inputs, outputs and checks of the work a job runs over an existing BlockSci index."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from exporters.artifact_paths import (
    BLOCKSCI_ANALYSIS_DIR,
    BLOCKSCI_CUSTOM_ANALYSIS_DIR,
    BLOCKSCI_NOTEBOOKS_DIR,
    DUMPLINGS_SNAPSHOT_FILE,
    REPORT_DIR,
)

from coinjoin_pipeline.configuration import BlockSciConfiguration, required

from .scripts import validated_s3_uri
from .stage_settings import AnalysisModeSettings, InputSettings, OutputSettings
from .validation import require_storage_path

# Uploaded after the job, and given the index provenance.
MODE_OUTPUTS = {
    "blocksci-analyze": BLOCKSCI_ANALYSIS_DIR,
    "blocksci-script": BLOCKSCI_CUSTOM_ANALYSIS_DIR,
    "blocksci-external": REPORT_DIR,
    "blocksci-notebook": BLOCKSCI_NOTEBOOKS_DIR,
}


def analysis_mode_settings(mode: str, blocksci: BlockSciConfiguration) -> AnalysisModeSettings:
    """Settings of detection, an external comparison, a user script or a notebook (blocksci_jobs.blocksci_analysis)."""
    output: OutputSettings = {"relative": MODE_OUTPUTS[mode]}
    inputs: list[InputSettings] = []
    settings: AnalysisModeSettings = {"output": output, "inputs": inputs, "output_check": None}
    if mode == "blocksci-analyze":
        artifact = f"{BLOCKSCI_ANALYSIS_DIR}/blocksci_analysis.json"
        inputs += [{"run": ".pipeline/exporters"}, {"run": "coinjoin_emulator_data"}]
        settings["output_check"] = [artifact, f"Reusable BlockSci analysis did not produce {artifact}"]
    elif mode == "blocksci-external":
        baseline_uri = validated_s3_uri(
            required(blocksci.external_baseline_uri, "--external-baseline-uri"), "Dumplings baseline URI"
        )
        settings["output_check"] = [
            f"{REPORT_DIR}/unified_report.json",
            "External BlockSci report did not produce unified_report.json",
        ]
        inputs += [
            {"run": ".pipeline/exporters"},
            {"uri": baseline_uri, "to": "coinjoin-analysis_data/coinjoin_tx_info.json"},
        ]
        if baseline_uri.endswith("/coinjoin_tx_info.json"):
            # A Dumplings baseline keeps the snapshot it came from next to it; optional.
            name = PurePosixPath(DUMPLINGS_SNAPSHOT_FILE).name
            inputs.append(
                {"uri": baseline_uri.rsplit("/", 1)[0] + "/" + name, "to": DUMPLINGS_SNAPSHOT_FILE, "optional": True}
            )
        if blocksci.external_false_cjtxs_uri is not None:
            # The report filters the baseline with every false_cjtxs.json* it finds there.
            inputs.append(
                {
                    "uri": validated_s3_uri(blocksci.external_false_cjtxs_uri, "false-positive list URI"),
                    "to": "coinjoin-analysis_data/false_cjtxs.json",
                }
            )
    elif mode == "blocksci-script":
        # The configuration rules checked that the script exists; the node reads it from shared storage.
        script_path = Path(required(blocksci.script, "--blocksci-script")).expanduser().resolve()
        require_storage_path(script_path)
        settings["user_script"] = str(script_path)
    else:
        # The execution node can set HOME to another storage volume; the submission home
        # is where the frontend stages files and looks for readiness.
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
