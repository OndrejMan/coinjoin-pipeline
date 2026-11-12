"""What the BlockSci jobs share: the run's working index and the persistent one.

A run-local index lives in <run>/blocksci_data; the container sees the run under
CONTAINER_RUNS_ROOT. A reusable index is published to <run prefix>/blocksci-parse_data
as an archive, its checksum and, last, its manifest. A persistent index stays in a
shared directory (index_store.py) and only its manifest is copied to the run.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from exporters.artifact_paths import BLOCKSCI_CONFIG_FILE, BLOCKSCI_DIR

from coinjoin_pipeline.execution.pbs.index_store import MANIFEST_SCHEMA, IndexStore
from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import PersistentSettings


# The archive of a reusable index in <run prefix>/blocksci-parse_data.
INDEX_ARCHIVE = "blocksci_data.tar.zst"

# Files without which a parsed index is unusable.
INDEX_FILES = (BLOCKSCI_CONFIG_FILE, BLOCKSCI_DIR + "/parsed/chain/block.dat")


def container_run(stage: Stage) -> str:
    return f"{CONTAINER_RUNS_ROOT}/{stage.run_id}"


def require_tools(*tools: str) -> None:
    for tool in tools:
        if shutil.which(tool) is None:
            raise StageFailure(f"{tool} is required to pack or unpack the BlockSci cache")


def canonicalize_index(stage: Stage) -> None:
    """An index copied from another location still names its old parsed directory."""
    config = stage.work / BLOCKSCI_CONFIG_FILE
    parsed = f"{container_run(stage)}/{BLOCKSCI_DIR}/parsed"
    text, replaced = re.subn(
        r'("dataDirectory"\s*:\s*)"[^"]*"', lambda match: f'{match.group(1)}"{parsed}"', config.read_text()
    )
    if not replaced:
        raise StageFailure("Could not canonicalize BlockSci dataDirectory")
    config.write_text(text)


def check_manifest(manifest: dict, label: str, image_name: str, run_id: str | None = None) -> None:
    """Reject a cache or index manifest of another schema, run or BlockSci image."""
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise StageFailure(f"{label} manifest has an unsupported schema")
    if run_id is not None and manifest.get("run_id") != run_id:
        raise StageFailure(f"{label} run ID does not match {run_id}")
    if manifest.get("blocksci_image") != image_name:
        raise StageFailure(f"{label} was produced by a different BlockSci image")


def restore_index(
    stage: Stage, cache_dir: Path, prefix: str, run_id: str, label: str, check: Callable[[dict], None] | None = None
) -> dict:
    """Verify a published index's metadata, then stream it into the run's working index.

    Only small metadata lands in cache_dir. The .writing marker stays on any failure, so
    nothing reads an index whose whole stream was not extracted and verified.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path, sidecar = cache_dir / "manifest.json", cache_dir / f"{INDEX_ARCHIVE}.sha256"
    stage.storage.copy(f"{prefix}/manifest.json", str(manifest_path))
    stage.storage.copy(f"{prefix}/{INDEX_ARCHIVE}.sha256", str(sidecar))
    manifest = json.loads(manifest_path.read_text())
    check_manifest(manifest, label, stage.settings["image_name"], run_id)
    if manifest.get("archive") != INDEX_ARCHIVE:
        raise StageFailure(f"{label} has an unsupported archive")
    if check is not None:
        check(manifest)
    checksum = sidecar.read_text().split(" ", 1)[0].strip()
    if manifest.get("archive_sha256") != checksum:
        raise StageFailure(f"{label} manifest SHA-256 does not match its sidecar")
    (stage.work / BLOCKSCI_DIR).mkdir(parents=True, exist_ok=True)
    writing = stage.work / BLOCKSCI_DIR / ".writing"
    writing.touch()
    stage.storage.restore_archive(f"{prefix}/{INDEX_ARCHIVE}", stage.work, checksum)
    for relative in INDEX_FILES:
        stage.require_file(relative, f"{label} did not contain {relative}")
    canonicalize_index(stage)
    writing.unlink()
    return manifest


def open_index(stage: Stage, settings: PersistentSettings, *, writer: bool) -> IndexStore:
    """Lock the persistent index for reading or writing; finalization releases the lock."""
    require_tools("zstd")
    index = IndexStore(
        str(settings["index_dir"]),
        settings.get("backup_uri"),
        storage=stage.storage,
        lock_seconds=int(settings["lock_seconds"]),
        wait_seconds=float(settings["wait_seconds"]),
        poll_seconds=float(settings["poll_seconds"]),
    )
    index.path.mkdir(parents=True, exist_ok=True)
    stage.finalizers.append(index.unlock)
    if writer:
        index.lock_exclusive()
    else:
        index.lock_shared()
    return index
