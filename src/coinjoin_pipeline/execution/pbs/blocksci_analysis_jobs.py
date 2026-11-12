"""The S3 PBS job that reads a BlockSci index: detection or a user script.

The index comes from this run's cache or read-only from another run's.
"""

from __future__ import annotations

import hashlib
import shutil
import socket
from pathlib import Path
from typing import cast

from exporters.artifact_paths import BLOCKSCI_CONFIG_FILE, BLOCKSCI_PARSE_DIR, STAGED_EXPORTERS_DIR

from coinjoin_pipeline.execution.pbs.blocksci_index import (
    container_run,
    require_tools,
    restore_index,
)
from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage
from coinjoin_pipeline.execution.pbs.stage_settings import BlocksciAnalysisSettings, OutputSettings


def analysis_output(stage: Stage, output: OutputSettings) -> Path:
    """Where the mode writes: its run directory in the job's scratch."""
    return stage.work / str(output["relative"])


def blocksci_analysis(stage: Stage) -> None:
    """Detection or a user script over an existing index.

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
    stage.log(f"[{mode}] starting on {socket.getfqdn()}")
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
