"""Persistent BlockSci index shared by PBS jobs (scenario-lab/mainnet-analysis-plan-v2.md §2.6).

Standard library only and Python 3.8 syntax: jobs ship this file next to their worker.

The working copy (multijob scratch or /storage) holds parsed/, config.json,
manifest.json and files.txt (every other file with its size). It is complete when it
has no .writing marker and its files match files.txt.

The backup prefix holds one backup: blocksci_index.tar.zst and manifest.json, written
last. A new backup deletes the old one first, so S3 never holds two copies; until its
manifest exists there is no valid backup.

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
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coinjoin_pipeline.storage.s5client import S5Client

ARCHIVE = "blocksci_index.tar.zst"
# What a backup holds; everything else in the working copy is lock or job state.
MEMBERS = ("config.json", "files.txt", "manifest.json", "parsed")


class IndexStoreError(RuntimeError):
    """The index cannot be locked, is incomplete, or its backup is unusable."""


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

    def publish(self, config: Path, manifest: dict) -> None:
        """Record a finished parse or update: config, file list and manifest, then completeness."""
        shutil.copyfile(config, self.path / "config.json")
        for name, content in (
            ("files.txt", inventory(self.path)),
            ("manifest.json", json.dumps(manifest, indent=2) + "\n"),
        ):
            temporary = self.path / f"{name}.tmp"
            temporary.write_text(content)
            os.replace(temporary, self.path / name)
        (self.path / ".writing").unlink(missing_ok=True)

    def link_into_run(self, run_work: Path) -> None:
        """The run-local config names the shared parsed/ through a symlink the job binds at the same path."""
        (run_work / "blocksci_data").mkdir(parents=True, exist_ok=True)
        (self.path / "parsed").mkdir(parents=True, exist_ok=True)
        link = run_work / "blocksci_data/parsed"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(self.path / "parsed")

    def binds(self, mode: str) -> list[str]:
        """Bind the index, and its target too when it is an alias.

        MetaCentrum's /scratch/multijob-* aliases point outside /scratch, which the node binds
        into every container: the alias alone resolves to nothing there and the bind is lost.
        """
        real = os.path.realpath(self.path)
        binds = [f"{real}:{real}:{mode}"]
        if real != str(self.path):
            binds.append(f"{real}:{self.path}:{mode}")
        return binds

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
        """Replace the S3 backup with the working copy; run under at least the shared lock.

        Only the object size is checked after the upload; restore verifies the SHA-256.
        """
        storage = self._storage()
        archive = f"{self.backup_uri}/{ARCHIVE}"
        print(f"[index] replacing the S3 backup in {self.backup_uri} (height {self.height()})", flush=True)
        storage.remove(f"{self.backup_uri}/manifest.json")
        storage.remove(archive, f"{archive}.sha256")
        checksum, size = storage.upload_archive(self.path, MEMBERS, archive, threads)
        storage.copy_text(f"{checksum}  {ARCHIVE}\n", f"{archive}.sha256")
        listed = storage.object_size(archive)
        if isinstance(listed, bool) or listed != size:
            raise IndexStoreError("Index backup S3 size/key mismatch")
        manifest = {
            **self.manifest(),
            "archive_uri": archive,
            "archive_sha256": checksum,
            "archive_size": size,
            "upload_validation": "size-only",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        storage.copy_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", f"{self.backup_uri}/manifest.json")
        print("[index] S3 backup complete", flush=True)

    def restore(self) -> None:
        """Under the writer lock: replace the working copy with the S3 backup."""
        storage = self._storage()
        manifest = storage.cat_json(f"{self.backup_uri}/manifest.json")
        archive, checksum = manifest.get("archive_uri"), manifest.get("archive_sha256")
        if archive != f"{self.backup_uri}/{ARCHIVE}" or not re.fullmatch(r"[0-9a-f]{64}", str(checksum)):
            raise IndexStoreError("Invalid index backup manifest")
        print(f"[index] restoring the working copy from {archive}", flush=True)
        self.begin_write()
        for name in ("parsed", "config.json", "files.txt", "manifest.json"):
            path = self.path / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
        storage.restore_archive(archive, self.path, str(checksum))
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
