"""The S3 PBS job that reads a BlockSci index: detection, a user script or a notebook.

The index comes from this run's cache or read-only from another run's.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import cast

from exporters.artifact_paths import BLOCKSCI_CONFIG_FILE, BLOCKSCI_PARSE_DIR, STAGED_EXPORTERS_DIR

from coinjoin_pipeline.execution.pbs.blocksci_index import (
    container_run,
    require_tools,
    restore_index,
)
from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import BlocksciAnalysisSettings, OutputSettings
from coinjoin_pipeline.storage.s5client import S5Error


def submission_home() -> Path:
    """The submitting user's home: the node may set HOME to another storage volume."""
    return Path(os.environ.get("PBS_O_HOME") or os.environ["HOME"])


def notebook_control(stage: Stage) -> Path:
    """Where the notebook publishes its connection details for the frontend."""
    return submission_home() / ".local/share/coinjoin-pipeline/jupyter" / stage.run_id


def analysis_output(stage: Stage, output: OutputSettings) -> Path:
    """Where the mode writes; a notebook writes to the submitting user's home by default."""
    if output.get("notebooks_dir"):
        return Path(str(output["notebooks_dir"]))
    if output.get("notebook"):
        return submission_home() / "coinjoin-notebooks" / stage.run_id
    return stage.work / str(output["relative"])


@contextmanager
def notebook_connection(stage: Stage, control: Path) -> Iterator[None]:
    """Publish the current job's private connection while Jupyter is running.

    The frontend container has no shared /storage mount. Its only discovery path is
    this S3 object, checked against qstat before opening a direct node SSH tunnel.
    """
    uri = f"{stage.run_prefix}/.notebook/connection.json"
    stopping = threading.Event()

    def publish() -> None:
        warned = False
        while not stopping.wait(0.2):
            try:
                path = control / "connection.json"
                if not path.is_file():
                    continue
                info = json.loads(path.read_text())
                if info.get("jobId") != os.environ.get("PBS_JOBID") or info.get("runId") != stage.run_id:
                    continue
                stage.storage.copy(path, uri, timeout=15)
                return
            except (OSError, ValueError, subprocess.SubprocessError, S5Error):
                if not warned:
                    stage.log("Notebook connection publication failed; retrying (connection token is not logged)")
                    warned = True
                stopping.wait(1)

    thread = threading.Thread(target=publish, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopping.set()
        thread.join(timeout=16)
        # Stale objects are also rejected by run ID, PBS job ID and terminal qstat state.
        # Wait for an in-flight upload before deleting, so it cannot republish afterwards.
        if not thread.is_alive():
            try:
                if not stage.storage.remove(uri, timeout=15):
                    stage.log("Could not remove ended notebook connection metadata")
            except (OSError, subprocess.SubprocessError, S5Error):
                stage.log("Could not remove ended notebook connection metadata")


def blocksci_analysis(stage: Stage) -> None:
    """Detection, a user script or a notebook over an existing index.

    Outputs are published even when the work fails. The index comes from this run's cache
    or read-only from another run's.
    """
    settings = cast(BlocksciAnalysisSettings, stage.settings)
    mode = stage.name
    output = analysis_output(stage, settings["output"])
    stage.finalizers.append(lambda: stage.upload(output, settings["output"]["relative"]))
    require_tools("zstd")
    stage.log(f"[{mode}] downloading required reusable BlockSci inputs")
    for download in settings["inputs"]:
        stage.download(download["run"])
    (stage.work / STAGED_EXPORTERS_DIR).mkdir(parents=True, exist_ok=True)
    binds = [f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw", f"{stage.work / STAGED_EXPORTERS_DIR}:/mnt/exporters:ro"]
    environment = [
        f"PBS_RUN_ID={stage.run_id}",
        f"ACTIVE_RUN_ID={stage.run_id}",
        f"BLOCKSCI_CONFIG={container_run(stage)}/{BLOCKSCI_CONFIG_FILE}",
        f"BLOCKSCI_RUN_DIR={container_run(stage)}",
    ]
    if settings["output"].get("notebooks_dir") and not output.is_dir():
        raise StageFailure(f"BlockSci notebooks directory does not exist on the compute node: {output}")
    output.mkdir(parents=True, exist_ok=True)
    restore_index(
        stage,
        stage.work / BLOCKSCI_PARSE_DIR,
        f"{stage.run_prefix}/{BLOCKSCI_PARSE_DIR}",
        stage.run_id,
        "BlockSci index",
    )
    if settings.get("user_script"):
        script = Path(str(settings["user_script"]))
        shutil.copyfile(script, output / "script.py")
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        (output / "script.py.sha256").write_text(f"{digest}  {script}\n")
        binds.append(f"{script}:/mnt/user-analysis.py:ro")
    control = None
    if settings["output"].get("notebook"):
        control = notebook_control(stage)
        control.mkdir(parents=True, exist_ok=True)
        control.chmod(0o700)
        (control / "connection.json").unlink(missing_ok=True)
        notebook = Path(__file__).parent / "templates/blocksci_notebook.py"
        binds += [
            f"{output}:/mnt/notebooks:rw",
            f"{control}:/mnt/notebook-control:rw",
            f"{notebook}:/mnt/blocksci_notebook.py:ro",
        ]
        environment += [
            f"NOTEBOOK_JOB_ID={os.environ['PBS_JOBID']}",
            f"NOTEBOOK_FRONTEND={os.environ.get('PBS_O_HOST', '')}",
            f"NOTEBOOK_LOGIN={os.environ.get('PBS_O_LOGNAME') or os.environ.get('USER', '')}",
        ]
    stage.log(f"[{mode}] starting on {socket.getfqdn()}")
    for line in settings.get("connection_help", ()):
        stage.log(line)
    with notebook_connection(stage, control) if control is not None else nullcontext():
        stage.singularity(
            f'cd "{CONTAINER_RUNS_ROOT}/$PBS_RUN_ID" && {settings["command"]}',
            binds=binds,
            environment=environment,
            cleanenv=True,
        )
    check = settings.get("output_check")
    if check:
        stage.require_file(check[0], check[1])
    stage.log(f"[{mode}] completed")
