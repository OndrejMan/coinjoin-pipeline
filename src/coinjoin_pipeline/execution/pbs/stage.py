"""One S3 PBS job on its compute node: its run tree on scratch, its S3 run prefix and its markers.

Whatever happens, the job writes and uploads ``.pbs/<stage>.done`` or
``.pbs/<stage>.failed`` (the frontend waits on them) after its outputs; finalizers
are the uploads that run first, whatever the outcome.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from exporters.artifact_paths import LOGS_DIR

from coinjoin_pipeline.execution.pbs.apptainer import MISSING_RUNTIME, exec_command, find_runtime
from coinjoin_pipeline.execution.pbs.stage_settings import StageSettings
from coinjoin_pipeline.storage.s5client import S5Client, pipe

# Where the containers see the runs: exporters read runs below the first root, the
# coinjoin-analysis collector scans every run below the second.
CONTAINER_RUNS_ROOT = "/runs/emulation/logs"
CONTAINER_SELECTED_ROOT = "/runs/emulation/selected"


def failure_status(error: BaseException) -> int:
    """A failed command's own exit status, else 1."""
    if isinstance(error, subprocess.CalledProcessError) and error.returncode > 0:
        return error.returncode
    return 1


class StageFailure(Exception):
    """A precondition or output check failed; the message is the job's diagnosis."""


class Stage:
    """One job's run tree on scratch, its S3 run prefix and its terminal markers."""

    def __init__(self, settings: StageSettings, scratch: Path) -> None:
        self.settings = settings
        self.name = settings["stage"]
        self.run_id = settings["run_id"]
        self.run_prefix = settings["run_prefix"]
        self.scratch = scratch
        self.runs_root = scratch / "coinjoin-run"
        self.work = self.runs_root / self.run_id
        self.storage = S5Client(settings["endpoint"], settings["credentials"], settings["profile"])
        # Uploads that run after the job body whatever its outcome, before the markers.
        self.finalizers: list[Callable[[], None]] = []

    def log(self, message: str) -> None:
        print(f"[{self.name}] {message}", flush=True)

    def download(self, name: str, destination: Path | None = None) -> None:
        """Sync one run directory from the run prefix, by default into the same place of the run tree."""
        target = self.work / name if destination is None else destination
        target.mkdir(parents=True, exist_ok=True)
        self.storage.run("sync", f"{self.run_prefix}/{name}/*", f"{target}/", check=True, timeout=None)

    def download_optional(self, name: str, destination: Path) -> None:
        """Like download, but a run directory no earlier stage produced is skipped."""
        if self.storage.listing(f"{self.run_prefix}/{name}/*"):
            self.download(name, destination)

    def upload(self, source: Path, name: str) -> None:
        self.storage.run("sync", f"{source}/", f"{self.run_prefix}/{name}/", check=True, timeout=None)

    def require_file(self, relative: str, message: str) -> None:
        if not (self.work / relative).is_file():
            raise StageFailure(message)

    def image_command(
        self,
        *command: str,
        binds: Sequence[str] = (),
        environment: Sequence[str] = (),
        cleanenv: bool = False,
    ) -> list[str]:
        """The Apptainer ``exec`` of command in the stage image, with these binds and variables."""
        runtime = find_runtime()
        if runtime is None:
            raise StageFailure(MISSING_RUNTIME)
        return exec_command(
            runtime, self.settings["image"], command, binds=binds, environment=environment, cleanenv=cleanenv
        )

    def singularity(
        self,
        script: str,
        *,
        binds: Sequence[str] = (),
        environment: Sequence[str] = (),
        cleanenv: bool = False,
        log: Path | None = None,
    ) -> None:
        """Run a shell script in the stage image; with log, its output is also kept in that file."""
        arguments = self.image_command("bash", "-c", script, binds=binds, environment=environment, cleanenv=cleanenv)
        if log is None:
            subprocess.run(arguments, check=True)
            return
        with log.open("wb") as kept:
            with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
                for line in iter(pipe(process.stdout).readline, b""):
                    sys.stdout.buffer.write(line)
                    sys.stdout.flush()
                    kept.write(line)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, arguments)

    def container(self, root: str, binds: Sequence[str], command: str) -> None:
        """Run command from the run directory below root as the container sees it."""
        self.singularity(
            f'cd "{root}/$PBS_RUN_ID" && {command}',
            binds=binds,
            environment=(f"PBS_RUN_ID={self.run_id}",),
        )

    def finalize(self, status: int) -> int:
        """Run every finalizer; a failing one fails a job that had succeeded."""
        for finalizer in self.finalizers:
            try:
                finalizer()
            except Exception as error:
                print(f"[{self.name}] finalization failed: {error}", file=sys.stderr)
                status = status or failure_status(error)
        return status

    def publish_marker(self, outcome: str) -> None:
        marker = self.work / ".pbs" / f"{self.name}.{outcome}"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(outcome + "\n")
        self.storage.copy(marker, f"{self.run_prefix}/.pbs/{self.name}.{outcome}")

    def finish(self, status: int) -> int:
        """Publish the terminal marker; a done marker that fails to upload turns into a failure."""
        if status == 0:
            try:
                self.publish_marker("done")
            except Exception as error:
                print(f"[{self.name}] done marker upload failed: {error}", file=sys.stderr)
                status = failure_status(error)
        if status != 0:
            try:
                self.publish_marker("failed")
            except Exception as error:
                print(f"[{self.name}] failed marker upload failed: {error}", file=sys.stderr)
        return status


def with_job_log(stage: Stage, name: str) -> None:
    """Copy the job's output into a log the finalization uploads beside the run's other logs.

    The output still reaches the PBS output file too, so a job submitted from a host
    PBS cannot copy that file back to loses nothing that S3 does not keep.
    """
    log = stage.work / LOGS_DIR / name
    log.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    reader, writer = os.pipe()
    os.dup2(writer, 1)
    os.dup2(writer, 2)
    os.close(writer)

    def copy() -> None:
        while chunk := os.read(reader, 65536):
            for target in (descriptor, saved[0]):
                try:
                    os.write(target, chunk)
                except OSError:
                    pass

    copier = threading.Thread(target=copy, name="job-log", daemon=True)
    copier.start()

    def restore_and_upload() -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        # A daemon a stage left running may still hold the pipe; upload what arrived.
        copier.join(timeout=10)
        stage.storage.copy(log, f"{stage.settings['log_prefix']}/{name}")

    # Before every later finalizer, the index lock release included.
    stage.finalizers.insert(0, restore_and_upload)
