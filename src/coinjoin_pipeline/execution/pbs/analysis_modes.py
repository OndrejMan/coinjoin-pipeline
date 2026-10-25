"""Inputs, mounts and output checks for work over an existing BlockSci index."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from exporters.artifact_paths import (
    BLOCKSCI_ANALYSIS_DIR,
    BLOCKSCI_CUSTOM_ANALYSIS_DIR,
    BLOCKSCI_NOTEBOOKS_DIR,
    REPORT_DIR,
)

from coinjoin_pipeline.storage.s3 import shell_value

from .scripts import download_run_directory, template, validated_s3_uri
from .validation import PBSError, require_existing_path, require_storage_path

# Uploaded after the job, and given the index provenance.
MODE_OUTPUTS = {
    "blocksci-analyze": BLOCKSCI_ANALYSIS_DIR,
    "blocksci-script": BLOCKSCI_CUSTOM_ANALYSIS_DIR,
    "blocksci-external": REPORT_DIR,
    "blocksci-notebook": BLOCKSCI_NOTEBOOKS_DIR,
}


@dataclass
class AnalysisMode:
    """Script fragments prepared before the shared index/cache setup is added."""

    output: str
    prepare: list[str]
    downloads: list[str]
    extra_binds: str
    connection_help: str
    output_check: str


def prepare_analysis_mode(
    mode: str,
    *,
    user_script: Path | None,
    external_baseline_uri: str | None,
    external_false_cjtxs_uri: str | None,
    notebooks_dir: Path | None,
    notebook_port: int,
) -> AnalysisMode:
    """Prepare detection, external comparison, a user script or a notebook."""
    output = f'"$RUN_WORK/{MODE_OUTPUTS[mode]}"'
    prepare = ['mkdir -p "$OUTPUT_DIR"']
    extra_binds = ""
    connection_help = ""
    output_check = ""
    downloads: list[str] = []
    if mode == "blocksci-analyze":
        artifact = "blocksci-analysis_data/blocksci_analysis.json"
        output_check = f'require_file "$RUN_WORK/{artifact}" "Reusable BlockSci analysis did not produce {artifact}"'
        downloads += [download_run_directory(".pipeline/exporters"), download_run_directory("coinjoin_emulator_data")]
    elif mode == "blocksci-external":
        if external_baseline_uri is None:
            raise PBSError("External BlockSci report requires a Dumplings baseline URI")
        baseline_uri = validated_s3_uri(external_baseline_uri, "Dumplings baseline URI")
        output_check = (
            'require_file "$RUN_WORK/coinjoinPipeline_data/unified_report.json" '
            '"External BlockSci report did not produce unified_report.json"'
        )
        downloads += [
            download_run_directory(".pipeline/exporters"),
            'mkdir -p "$RUN_WORK/coinjoin-analysis_data"',
            f's5 cp {shell_value(baseline_uri)} "$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json"',
        ]
        if baseline_uri.endswith("/coinjoin_tx_info.json"):
            # A Dumplings baseline keeps the snapshot it came from next to it; optional.
            snapshot = shell_value(baseline_uri.rsplit("/", 1)[0] + "/dumplings-snapshot.json")
            downloads.append(
                f's5 cp {snapshot} "$RUN_WORK/coinjoin-analysis_data/dumplings-snapshot.json"'
                ' || echo "[blocksci-external] the baseline has no dumplings-snapshot.json"'
            )
        if external_false_cjtxs_uri is not None:
            false_cjtxs = validated_s3_uri(external_false_cjtxs_uri, "false-positive list URI")
            # The report filters the baseline with every false_cjtxs.json* it finds there.
            downloads.append(f's5 cp {shell_value(false_cjtxs)} "$RUN_WORK/coinjoin-analysis_data/false_cjtxs.json"')
    elif mode == "blocksci-script":
        if user_script is None:
            raise PBSError("Reusable BlockSci script mode requires --blocksci-script")
        script_path = user_script.expanduser().resolve()
        require_storage_path(script_path)
        require_existing_path(script_path, "BlockSci user script")
        if not script_path.is_file():
            raise PBSError(f"BlockSci user script is not a file: {script_path}")
        prepare += [
            f"USER_SCRIPT={shell_value(str(script_path))}",
            'cp "$USER_SCRIPT" "$OUTPUT_DIR/script.py"',
            'sha256sum "$USER_SCRIPT" > "$OUTPUT_DIR/script.py.sha256"',
        ]
        extra_binds = 'EXTRA_BINDS+=(--bind "$USER_SCRIPT:/mnt/user-analysis.py:ro")'
    else:
        output = '"$HOME/coinjoin-notebooks/$RUN_ID"'
        if notebooks_dir is not None:
            notebook_path = notebooks_dir.expanduser().resolve()
            require_storage_path(notebook_path)
            require_existing_path(notebook_path, "BlockSci notebooks directory")
            if not notebook_path.is_dir():
                raise PBSError(f"BlockSci notebooks path is not a directory: {notebook_path}")
            output = shell_value(str(notebook_path))
        prepare += [
            'NOTEBOOK_CONTROL="$HOME/.local/share/coinjoin-pipeline/jupyter/$RUN_ID"',
            'mkdir -p "$NOTEBOOK_CONTROL"',
            'chmod 700 "$NOTEBOOK_CONTROL"',
            'rm -f "$NOTEBOOK_CONTROL/connection.json"',
            "cat > \"$RUN_WORK/blocksci_notebook.py\" <<'SCENARIO_LAB_NOTEBOOK'\n"
            + template("blocksci_notebook.py")
            + "\nSCENARIO_LAB_NOTEBOOK",
        ]
        extra_binds = (
            'EXTRA_BINDS+=(--bind "$OUTPUT_DIR:/mnt/notebooks:rw" '
            '--bind "$NOTEBOOK_CONTROL:/mnt/notebook-control:rw" '
            '--bind "$RUN_WORK/blocksci_notebook.py:/mnt/blocksci_notebook.py:ro" '
            '--env "NOTEBOOK_JOB_ID=${PBS_JOBID:?}" '
            '--env "NOTEBOOK_FRONTEND=${PBS_O_HOST:-}" '
            '--env "NOTEBOOK_LOGIN=${PBS_O_LOGNAME:-$USER}")'
        )
        connection_help = (
            f'echo "[blocksci-notebook] Preferred Jupyter port: {notebook_port}"\n'
            'echo "[blocksci-notebook] Open JupyterLab from Scenario Lab; connection details follow after startup."'
        )

    return AnalysisMode(
        output=output,
        prepare=prepare,
        downloads=downloads,
        extra_binds=extra_binds,
        connection_help=connection_help,
        output_check=output_check,
    )
