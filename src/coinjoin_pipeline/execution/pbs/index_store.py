"""A parsed BlockSci index and its S3 backup.

Standard library only and Python 3.8 syntax, like every module a PBS job ships.

The working copy holds parsed/, config.json, manifest.json and files.txt (every other
file with its size). It is complete when it has no .writing marker and its files match
files.txt.

A run's own index in <run prefix>/blocksci-parse_data is one backup in the format of
s5client.py (replace_backup, restore_backup), of an index that lived in the job's
scratch: blocksci_index.tar.zst, its .sha256 and manifest.json, written last. A new
backup deletes the old one first, so S3 never holds two copies; until its manifest
exists there is no valid backup.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from coinjoin_pipeline.storage.s5client import S5Client

# Schema of the manifest every parsed index records.
MANIFEST_SCHEMA = "1.1"
ARCHIVE = "blocksci_index.tar.zst"
# What a backup holds; everything else in the working copy is job state.
MEMBERS = ("config.json", "files.txt", "manifest.json", "parsed")


class IndexStoreError(RuntimeError):
    """The index is incomplete, or its backup is unusable."""


def require_zstd() -> None:
    """Packing and unpacking the archive need zstd; reading an index in place does not."""
    if shutil.which("zstd") is None:
        raise IndexStoreError("[index] zstd is required to pack or unpack the index archive")


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
    S3 cache, the resources of the job.
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


def inventory(index: Path) -> str:
    """``<path> <size>`` of every regular file, sorted; files.txt records it.

    Job state, the record files themselves and temporaries are left out.
    """
    rows = []
    for directory, subdirectories, names in os.walk(index):
        here = Path(directory)
        if here == index:
            subdirectories[:] = [name for name in subdirectories if name != ".writing"]
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
    """One job's view of a parsed index: its working copy and backup."""

    def __init__(
        self,
        index_dir: str,
        backup_uri: str | None,
        *,
        storage: S5Client | None = None,
        job_id: str | None = None,
    ) -> None:
        self.path = Path(index_dir)
        self.backup_uri = (backup_uri or "").rstrip("/")
        self.storage = storage
        self.job_id = job_id or os.environ.get("PBS_JOBID") or f"pid-{os.getpid()}"

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

        The manifest gains when the index was recorded; it is returned as written.
        """
        manifest = {**manifest, "published_at": utc_now()}
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

    # Backup -----------------------------------------------------------------

    def _storage(self) -> S5Client:
        if self.storage is None or not self.backup_uri:
            raise IndexStoreError("[index] this job has no S3 backup of the index")
        return self.storage

    def backup(self, threads: int) -> None:
        """Replace the S3 backup with the working copy."""
        storage = self._storage()
        require_zstd()
        print(f"[index] replacing the S3 backup in {self.backup_uri} (height {self.height()})", flush=True)
        storage.replace_backup(self.path, MEMBERS, self.backup_uri, ARCHIVE, self.manifest(), threads)
        print("[index] S3 backup complete", flush=True)

    def discard(self) -> None:
        """Remove the working copy's data; the .writing marker stays."""
        for name in MEMBERS:
            path = self.path / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()

    def restore(self) -> None:
        """Replace the working copy with the S3 backup.

        The old copy goes first, so the index never needs its space twice. The backup is
        extracted beside the data and moved in only after its SHA-256 was verified.
        """
        storage = self._storage()
        require_zstd()
        print(f"[index] restoring the working copy from {self.backup_uri}", flush=True)
        self.begin_write()
        self.discard()
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
