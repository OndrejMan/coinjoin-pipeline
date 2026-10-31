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

from .scripts import validated_s3_uri
from .validation import PBSError, require_existing_path, require_storage_path

# Uploaded after the job, and given the index provenance.
MODE_OUTPUTS = {
    "blocksci-analyze": BLOCKSCI_ANALYSIS_DIR,
    "blocksci-script": BLOCKSCI_CUSTOM_ANALYSIS_DIR,
    "blocksci-external": REPORT_DIR,
    "blocksci-notebook": BLOCKSCI_NOTEBOOKS_DIR,
}


def analysis_mode_settings(
    mode: str,
    *,
    user_script: Path | None,
    external_baseline_uri: str | None,
    external_false_cjtxs_uri: str | None,
    notebooks_dir: Path | None,
    notebook_port: int,
) -> dict[str, object]:
    """Settings of detection, an external comparison, a user script or a notebook (stage_runtime.blocksci_analysis)."""
    output: dict[str, object] = {"relative": MODE_OUTPUTS[mode]}
    inputs: list[dict[str, object]] = []
    settings: dict[str, object] = {"output": output, "inputs": inputs, "output_check": None}
    if mode == "blocksci-analyze":
        artifact = f"{BLOCKSCI_ANALYSIS_DIR}/blocksci_analysis.json"
        inputs += [{"run": ".pipeline/exporters"}, {"run": "coinjoin_emulator_data"}]
        settings["output_check"] = [artifact, f"Reusable BlockSci analysis did not produce {artifact}"]
    elif mode == "blocksci-external":
        if external_baseline_uri is None:
            raise PBSError("External BlockSci report requires a Dumplings baseline URI")
        baseline_uri = validated_s3_uri(external_baseline_uri, "Dumplings baseline URI")
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
        if external_false_cjtxs_uri is not None:
            # The report filters the baseline with every false_cjtxs.json* it finds there.
            inputs.append(
                {
                    "uri": validated_s3_uri(external_false_cjtxs_uri, "false-positive list URI"),
                    "to": "coinjoin-analysis_data/false_cjtxs.json",
                }
            )
    elif mode == "blocksci-script":
        if user_script is None:
            raise PBSError("Reusable BlockSci script mode requires --blocksci-script")
        script_path = user_script.expanduser().resolve()
        require_storage_path(script_path)
        require_existing_path(script_path, "BlockSci user script")
        if not script_path.is_file():
            raise PBSError(f"BlockSci user script is not a file: {script_path}")
        settings["user_script"] = str(script_path)
    else:
        # The execution node can set HOME to another storage volume; the submission home
        # is where the frontend stages files and looks for readiness.
        output["notebook"] = True
        if notebooks_dir is not None:
            notebook_path = notebooks_dir.expanduser().resolve()
            require_storage_path(notebook_path)
            require_existing_path(notebook_path, "BlockSci notebooks directory")
            if not notebook_path.is_dir():
                raise PBSError(f"BlockSci notebooks path is not a directory: {notebook_path}")
            output["notebooks_dir"] = str(notebook_path)
        settings["connection_help"] = [
            f"[blocksci-notebook] Preferred Jupyter port: {notebook_port}",
            "[blocksci-notebook] Open JupyterLab from Scenario Lab; connection details follow after startup.",
        ]
    return settings
