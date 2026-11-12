"""Entry point of every S3 PBS job: ``python3 run.py settings.json`` on the compute node.

A job ships this module, the modules it imports and its ``settings.json`` to the
node's scratch at their paths below ``src/`` or ``pipeline/`` (``scripts.package_files``),
so they import each other there exactly as on the frontend. Standard library only and
Python 3.8 syntax.

Whatever happens, the job writes and uploads ``.pbs/<stage>.done`` or
``.pbs/<stage>.failed`` (the frontend waits on them) after its outputs, then empties
the job's scratch directory. The exit status is the work's; a failed output or marker
upload turns success into that upload's status.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from exporters.artifact_paths import LOGS_DIR, PBS_MARKER_DIR

from coinjoin_pipeline.execution.pbs import analyzer_jobs, blocksci_jobs
from coinjoin_pipeline.execution.pbs.stage import Stage, StageFailure, with_job_log
from coinjoin_pipeline.execution.pbs.stage_settings import StageSettings

JOBS: dict[str, Callable[[Stage], None]] = {
    "blocksci": blocksci_jobs.blocksci,
    "coinjoin-analysis": analyzer_jobs.coinjoin_analysis,
}


def clean_scratch(scratch: Path) -> None:
    """Empty the scratch once the markers are in S3; MetaCentrum would keep it for 14 days.

    Only the contents go: the system removes the directory itself.
    """
    if shutil.which("clean_scratch"):
        subprocess.run(["clean_scratch"], check=False)
        return
    if not scratch.is_dir():
        return
    for entry in scratch.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink()


def run(stage: Stage) -> int:
    """The job body; the exit status a shell job with ``set -e`` would end with."""
    try:
        if stage.settings.get("job_log"):
            # Before any check, so even a missing credential reaches the uploaded log.
            with_job_log(stage, str(stage.settings["job_log"]))
        if not os.access(stage.settings["credentials"], os.R_OK):
            raise StageFailure(f"S3 credentials file is not readable: {stage.settings['credentials']}")
        if shutil.which("s5cmd") is None:
            raise StageFailure("s5cmd is required")
        for directory in (stage.work / PBS_MARKER_DIR, stage.work / LOGS_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        JOBS[stage.settings["job"]](stage)
    except subprocess.CalledProcessError as error:
        print(f"[{stage.name}] {error}", file=sys.stderr)
        return error.returncode or 1
    except Exception as error:
        # Any failure, a lock or transfer error included, must still publish the failed marker.
        print(f"[{stage.name}] {error}", file=sys.stderr)
        return 1
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    scratch = os.environ.get("SCRATCHDIR")
    if not scratch:
        print("SCRATCHDIR is not set", file=sys.stderr)
        return 1
    settings: StageSettings = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    for name in ("TMPDIR", "SINGULARITY_CACHEDIR", "SINGULARITY_TMPDIR", "SINGULARITY_LOCALCACHEDIR"):
        os.environ[name] = scratch
    stage = Stage(settings, Path(scratch))

    def terminated(_signum: int, _frame: object) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminated)
    status = stage.finish(stage.finalize(run(stage)))
    clean_scratch(stage.scratch)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
