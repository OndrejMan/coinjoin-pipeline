"""What the BlockSci jobs share: the run's working index and the persistent one.

A run-local index lives in <run>/blocksci_data; the container sees the run under
CONTAINER_RUNS_ROOT. A reusable index is published to <run prefix>/blocksci-parse_data
as a backup in the format of the persistent index's (index_store.py): an archive, its
checksum and, last, its manifest. A persistent index stays in a shared directory and only
its manifest is copied to the run.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

from exporters.artifact_paths import BLOCKSCI_CONFIG_FILE, BLOCKSCI_DIR

from coinjoin_pipeline.execution.pbs.index_store import MANIFEST_SCHEMA, IndexStore
from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import PersistentSettings


# Files without which a parsed index is unusable.
INDEX_FILES = (BLOCKSCI_CONFIG_FILE, BLOCKSCI_DIR + "/parsed/chain/block.dat")


def container_run(stage: Stage) -> str:
    return f"{CONTAINER_RUNS_ROOT}/{stage.run_id}"


def require_tools(*tools: str) -> None:
    for tool in tools:
        if shutil.which(tool) is None:
            raise StageFailure(f"{tool} is required to pack or unpack the BlockSci cache")


def edit_config(stage: Stage, section: str, key: str, value: object) -> None:
    """Set one existing key of the run's BlockSci config.json, such as parser.maxBlockNum."""
    config = stage.work / BLOCKSCI_CONFIG_FILE
    try:
        content = json.loads(config.read_text())
        if key not in content[section]:
            raise KeyError(key)
        content[section][key] = value
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StageFailure(f"Could not set {section}.{key} in the BlockSci config") from error
    config.write_text(json.dumps(content, indent=4) + "\n")


def config_value(stage: Stage, section: str, key: str) -> object:
    """One key of the run's BlockSci config.json, or None when it has none."""
    try:
        content = json.loads((stage.work / BLOCKSCI_CONFIG_FILE).read_text())
        return content[section][key]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def canonicalize_index(stage: Stage) -> None:
    """Point config.json at parsed/ as this job's container sees it; a copied index still names its old one."""
    edit_config(stage, "chainConfig", "dataDirectory", f"{container_run(stage)}/{BLOCKSCI_DIR}/parsed")


def check_manifest(manifest: dict, label: str, image_name: str, run_id: str | None = None) -> None:
    """Reject a cache or index manifest of another schema, run or BlockSci image."""
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise StageFailure(f"{label} manifest has an unsupported schema")
    if run_id is not None and manifest.get("run_id") != run_id:
        raise StageFailure(f"{label} run ID does not match {run_id}")
    if manifest.get("blocksci_image") != image_name:
        raise StageFailure(f"{label} was produced by a different BlockSci image")


def run_index(stage: Stage, prefix: str) -> IndexStore:
    """The run's working index as an IndexStore whose backup is the run cache at prefix."""
    return IndexStore(str(stage.work / BLOCKSCI_DIR), prefix, storage=stage.storage)


def restore_index(
    stage: Stage, cache_dir: Path, prefix: str, run_id: str, label: str, check: Callable[[dict], None] | None = None
) -> dict:
    """Check a published index's manifest, then restore it into the run's working index.

    The restore is the persistent index's: the archive is hashed while it is extracted,
    only its declared members are accepted, and the restored files must match files.txt.
    The .writing marker stays on any failure, so nothing reads a partly restored index.
    The manifest is kept in cache_dir, where the job reads it.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / "manifest.json"
    stage.storage.copy(f"{prefix}/manifest.json", str(manifest_path))
    manifest = json.loads(manifest_path.read_text())
    check_manifest(manifest, label, stage.settings["image_name"], run_id)
    if check is not None:
        check(manifest)
    run_index(stage, prefix).restore()
    for relative in INDEX_FILES:
        stage.require_file(relative, f"{label} did not contain {relative}")
    canonicalize_index(stage)
    return manifest


def open_index(stage: Stage, settings: PersistentSettings, *, writer: bool) -> IndexStore:
    """Lock the persistent index for reading or writing; finalization releases the lock.

    Only restoring or backing it up needs zstd, which IndexStore checks when it does.
    """
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
