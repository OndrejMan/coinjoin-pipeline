"""Entry point of a mainnet synchronization PBS job: ``python3 run.py settings.json <stage>``.

The job ships this module and those it imports at their paths below ``src/`` or
``pipeline/`` (``pbs.scripts.package_files``), as the S3 stage worker does. Besides a stage, the
arguments can be ``failed <stage> <started>``, which the job's shell trap runs. Standard
library only and Python 3.8 syntax.
"""

from __future__ import annotations

import json
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from exporters.artifact_paths import pbs_marker

from coinjoin_pipeline.execution.mainnet_jobs import blocksci, checkpoint, producer
from coinjoin_pipeline.execution.mainnet_worker import Worker
from coinjoin_pipeline.execution.pbs.index_store import IndexStore

JOBS: dict[str, Callable[[Worker], None]] = {
    "producer": producer,
    "blocksci": blocksci,
    "checkpoint": checkpoint,
}


def main(argv: list[str] | None = None) -> None:
    args = argv or sys.argv[1:]
    settings = Path(args[0])
    worker = Worker(json.loads(settings.read_text(encoding="utf-8")), settings.parent)
    if args[1] == "failed":
        worker.stage = args[2][len("mainnet-") :] if args[2].startswith("mainnet-") else args[2]
        if (len(args) > 3 and args[3] == "0") or (worker.run / f"{worker.stage}.quiescent").is_file():
            worker.release_dataset()
        worker.progress("failed", state="failed", error="PBS stage failed; inspect the persistent job logs")
        failed = pbs_marker("mainnet-" + worker.stage, "failed")
        worker.publish_json(f"{worker.remote}/{failed}", {"run_id": worker.run_id})
        worker.report_storage()
        return
    worker.stage = args[1]
    job = JOBS[args[1]]
    # The fixed state directory may hold the marker of an earlier run of this stage.
    (worker.run / f"{worker.stage}.quiescent").unlink(missing_ok=True)

    def interrupted(_signum: int, _frame: object) -> None:
        raise InterruptedError("PBS job interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    # Fail at once on an active notebook or index job instead of holding an allocation waiting.
    index = IndexStore(worker.c["index_dir"], None, lock_seconds=worker.c.get("index_lock_seconds", 0), poll_seconds=1)
    try:
        mode = worker.c.get("index_lock")
        if mode == "exclusive":
            index.lock_exclusive()
        elif mode == "shared":
            index.lock_shared()
        job(worker)
    finally:
        index.unlock()
        try:
            worker.cleanup()
            worker.run.mkdir(parents=True, exist_ok=True)
            (worker.run / f"{worker.stage}.quiescent").touch()
        finally:
            worker.report_storage()
            try:
                worker.publish_logs()
            except Exception as error:
                print(f"Log upload failed; logs remain in {worker.run}: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
