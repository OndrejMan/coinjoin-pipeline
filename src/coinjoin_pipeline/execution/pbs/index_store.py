"""Persistent BlockSci index shared by PBS jobs.

Standard library only and Python 3.8 syntax, like every module a PBS job ships.

The working copy (multijob scratch or /storage) holds parsed/, config.json,
manifest.json and files.txt (every other file with its size). It is complete when it
has no .writing marker and its files match files.txt.

The backup prefix holds one backup in the format of s5client.py (replace_backup,
restore_backup): blocksci_index.tar.zst, its .sha256 and manifest.json, written last.
A new backup deletes the old one first, so S3 never holds two copies; until its
manifest exists there is no valid backup. The mainnet checkpoint job writes the same
backup, so either job's backup restores in the other.

Lock: one writer or many readers, as directories under <index>/.lock. Entries are built
aside and renamed into place, which is atomic on NFS and BeeGFS without flock. Each
holds its job ID and an expiry past the job's walltime, so an entry a killed job left
is cleared instead of blocking the index forever. scenario-lab's multijob cleanup
reads the same entries.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping

from exporters.artifact_paths import BLOCKSCI_DIR

from coinjoin_pipeline.execution.pbs.apptainer import alias_binds

if TYPE_CHECKING:
    from coinjoin_pipeline.storage.s5client import S5Client

# Schema of the manifest every parsed index records, an S3 cache and a persistent index alike.
MANIFEST_SCHEMA = "1.1"
ARCHIVE = "blocksci_index.tar.zst"
# What a backup holds; everything else in the working copy is lock or job state.
MEMBERS = ("config.json", "files.txt", "manifest.json", "parsed")


class IndexStoreError(RuntimeError):
    """The index cannot be locked, is incomplete, or its backup is unusable."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def index_manifest(
    run_id: str,
    blocksci_image: str,
    source_kind: str,
    network: str,
    exported_max_block: int,
    extra: Mapping[str, object] | None = None,
) -> dict:
    """What a parse or update records about the index it produced.

    An update resumes only an index of the same image, source kind and network, from
    exported_max_block. ``extra`` holds what one kind of index adds: the archive of an
    S3 cache, the block hash of a verified chain, the resources of the job.
    """
    return {
        "schema_version": MANIFEST_SCHEMA,
        "run_id": run_id,
        "blocksci_image": blocksci_image,
        "source_kind": source_kind,
        "network": network,
        "exported_max_block": exported_max_block,
        **(extra or {}),
    }


def directory_usage(root: Path) -> dict[str, int]:
    """Apparent size and file count of a tree; a missing directory is empty."""
    size = files = 0
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if not path.is_symlink():
                size += path.stat().st_size
                files += 1
    return {"bytes": size, "files": files}


def usage_report(stage: str, roots: Iterable[Path], **extra: object) -> dict[str, object]:
    """What a job tells the lab about the directories it keeps on the multijob."""
    directories = {str(root): directory_usage(root) for root in roots}
    return {**extra, "stage": stage, "measured_at": utc_now(), "directories": directories}


def inventory(index: Path) -> str:
    """``<path> <size>`` of every regular file, sorted; files.txt records it.

    Lock and job state, the record files themselves and temporaries are left out.
    """
    rows = []
    for directory, subdirectories, names in os.walk(index):
        here = Path(directory)
        if here == index:
            subdirectories[:] = [name for name in subdirectories if name not in {".lock", ".writing"}]
        for name in names:
            path = here / name
            if here == index and name == ".writing":
                continue
            if name in {"files.txt", "manifest.json"} or name.endswith(".tmp"):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            rows.append(f"{path.relative_to(index)} {path.lstat().st_size}")
    return "".join(row + "\n" for row in sorted(rows))


class IndexStore:
    """One job's view of a persistent index: its lock entry, working copy and backup."""

    def __init__(
        self,
        index_dir: str,
        backup_uri: str | None,
        *,
        storage: S5Client | None = None,
        job_id: str | None = None,
        lock_seconds: int = 0,
        wait_seconds: float = 0,
        poll_seconds: float = 60,
    ) -> None:
        self.path = Path(index_dir)
        self.backup_uri = (backup_uri or "").rstrip("/")
        self.storage = storage
        self.job_id = job_id or os.environ.get("PBS_JOBID") or f"pid-{os.getpid()}"
        # Past this moment the job is gone, so its lock entry may be cleared.
        self.expires = int(time.time()) + lock_seconds
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.lock = self.path / ".lock"
        self.held: Path | None = None

    # Lock -------------------------------------------------------------------

    def _create(self, target: Path) -> bool:
        staging = self.lock / f".new-{self.job_id}-{random.randrange(32768)}"
        staging.mkdir(parents=True)
        (staging / "job").write_text(f"{self.job_id}\n")
        (staging / "expires").write_text(f"{self.expires}\n")
        try:
            # rename() replaces only an empty directory, so an existing entry wins.
            os.rename(staging, target)
            return True
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            return False

    def clear_expired(self) -> None:
        now = int(time.time())
        readers = self.lock / "readers"
        entries = [self.lock / "writer", *(sorted(readers.iterdir()) if readers.is_dir() else [])]
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                expires = int((entry / "expires").read_text().strip())
            except (OSError, ValueError):
                expires = 0
            if now >= expires:
                try:
                    job = (entry / "job").read_text().strip()
                except OSError:
                    job = "unknown"
                print(f"[index] clearing expired lock {entry.name} of job {job}", flush=True)
                shutil.rmtree(entry, ignore_errors=True)

    def _wait(self, deadline: float, message: str) -> None:
        if time.time() >= deadline:
            raise IndexStoreError(message)
        time.sleep(self.poll_seconds)

    def lock_shared(self) -> None:
        deadline = time.time() + self.wait_seconds
        entry = self.lock / "readers" / self.job_id
        (self.lock / "readers").mkdir(parents=True, exist_ok=True)
        while True:
            self.clear_expired()
            if not (self.lock / "writer").is_dir() and self._create(entry):
                # A writer that arrived in between waits for readers; step back so it can proceed.
                if not (self.lock / "writer").is_dir():
                    self.held = entry
                    return
                shutil.rmtree(entry, ignore_errors=True)
            self._wait(deadline, f"[index] {self.path} is being updated; gave up after {self.wait_seconds:g}s")

    def lock_exclusive(self) -> None:
        deadline = time.time() + self.wait_seconds
        readers = self.lock / "readers"
        readers.mkdir(parents=True, exist_ok=True)
        while not self._create(self.lock / "writer"):
            self.clear_expired()
            self._wait(deadline, f"[index] another job writes {self.path}; gave up after {self.wait_seconds:g}s")
        self.held = self.lock / "writer"
        # New readers now step back; wait for the ones already reading.
        while any(readers.iterdir()):
            self.clear_expired()
            if time.time() >= deadline:
                self.unlock()
                raise IndexStoreError(f"[index] readers still hold {self.path} after {self.wait_seconds:g}s")
            time.sleep(self.poll_seconds)

    def unlock(self) -> None:
        if self.held is not None:
            shutil.rmtree(self.held, ignore_errors=True)
            self.held = None

    # Working copy -----------------------------------------------------------

    def complete(self) -> bool:
        files = self.path / "files.txt"
        return (
            (self.path / "manifest.json").is_file()
            and files.is_file()
            and not (self.path / ".writing").exists()
            and inventory(self.path) == files.read_text()
        )

    def manifest(self) -> dict:
        value = json.loads((self.path / "manifest.json").read_text())
        if not isinstance(value, dict):
            raise IndexStoreError(f"[index] {self.path}/manifest.json is not a JSON object")
        return value

    def height(self) -> object:
        return self.manifest().get("exported_max_block")

    def begin_write(self) -> None:
        """Mark the working copy incomplete until publish."""
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / ".writing").touch()

    def publish(self, config: Path, manifest: dict) -> dict:
        """Record a finished parse or update: config, file list and manifest, then completeness.

        The manifest gains where and when the index was recorded; it is returned as written.
        """
        manifest = {**manifest, "storage": "persistent", "index_dir": str(self.path), "published_at": utc_now()}
        if config.resolve() != (self.path / "config.json").resolve():
            shutil.copyfile(config, self.path / "config.json")
        for name, content in (
            ("files.txt", inventory(self.path)),
            ("manifest.json", json.dumps(manifest, indent=2) + "\n"),
        ):
            temporary = self.path / f"{name}.tmp"
            temporary.write_text(content)
            os.replace(temporary, self.path / name)
        (self.path / ".writing").unlink(missing_ok=True)
        return manifest

    def link_into_run(self, run_work: Path) -> None:
        """The run-local config names the shared parsed/ through a symlink the job binds at the same path."""
        (run_work / BLOCKSCI_DIR).mkdir(parents=True, exist_ok=True)
        (self.path / "parsed").mkdir(parents=True, exist_ok=True)
        link = run_work / BLOCKSCI_DIR / "parsed"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(self.path / "parsed")

    def binds(self, mode: str) -> list[str]:
        """Bind the index, and its target too when it is a multijob alias."""
        return alias_binds(self.path, mode)

    # Backup -----------------------------------------------------------------

    def _storage(self) -> S5Client:
        if self.storage is None or not self.backup_uri:
            raise IndexStoreError("[index] this job has no S3 backup of the index")
        return self.storage

    def backup_available(self) -> bool:
        return (
            bool(self.backup_uri)
            and self.storage is not None
            and self.storage.exists(f"{self.backup_uri}/manifest.json")
        )

    def backup(self, threads: int) -> None:
        """Replace the S3 backup with the working copy; run under at least the shared lock."""
        storage = self._storage()
        print(f"[index] replacing the S3 backup in {self.backup_uri} (height {self.height()})", flush=True)
        storage.replace_backup(self.path, MEMBERS, self.backup_uri, ARCHIVE, self.manifest(), threads)
        print("[index] S3 backup complete", flush=True)

    def discard(self) -> None:
        """Remove the working copy's data; the lock and the .writing marker stay."""
        for name in MEMBERS:
            path = self.path / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()

    def restore(self) -> None:
        """Under the writer lock: replace the working copy with the S3 backup.

        The old copy goes first, so the index never needs its space twice. The backup is
        extracted beside the data and moved in only after its SHA-256 was verified.
        """
        storage = self._storage()
        print(f"[index] restoring the working copy from {self.backup_uri}", flush=True)
        self.begin_write()
        self.discard()
        # A killed restore leaves its staging directory; the writer lock says none is in use.
        for leftover in self.path.glob(".restore-*"):
            shutil.rmtree(leftover, ignore_errors=True)
        staging = self.path / f".restore-{self.job_id}"
        try:
            storage.restore_backup(f"{self.backup_uri}/manifest.json", staging)
            for entry in staging.iterdir():
                entry.rename(self.path / entry.name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        (self.path / ".writing").unlink()
        if not self.complete():
            raise IndexStoreError("[index] the restored copy does not match its file list")

    def reconcile(self) -> None:
        """Under the writer lock: leave the working copy complete, from the S3 backup if needed."""
        if self.complete():
            return
        if not self.backup_available():
            raise IndexStoreError(
                f"[index] working copy {self.path} is incomplete and there is no S3 backup; parse again"
            )
        self.restore()
