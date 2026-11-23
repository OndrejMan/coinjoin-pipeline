"""Dumplings and the BlockSci index built again from the block archive after their backups were deleted."""
# ruff: noqa: F811  (tests take the imported fixtures as arguments)

import hashlib
import json
import os
import shutil
import subprocess
from collections import namedtuple

import pytest

from coinjoin_pipeline.execution import mainnet_jobs
from coinjoin_pipeline.execution.mainnet_worker import atomic_json
# The fixtures come from the synchronization tests; pytest finds them by these names.
from test_mainnet_sync import config, no_core_settle_wait, worker  # noqa: F401
from test_mainnet_sync import fake_components, synchronize, wipe_working_dataset

pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
PAYLOAD = b"archived block " * 300
MTIME = 1_700_000_000_123_456_789


def archive_directory(worker, tmp_path):
    return tmp_path / "s3" / worker.c["blocks_uri"].removeprefix("s3://")


def archive_oldest_file(worker, tmp_path, packed=True):
    """What the archiver, and with ``packed`` the pack job, leave for blk00000.dat before pruning."""
    block = worker.core_dir / "blocks/blk00000.dat"
    block.write_bytes(PAYLOAD)
    os.utime(block, ns=(MTIME, MTIME))
    sidecar = {
        "schema_version": 1,
        "file": "blk00000.dat",
        "size": len(PAYLOAD),
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        "height_ranges": [[0, 10]],
    }
    uri = worker.c["blocks_uri"].rstrip("/") + "/blk00000.dat"
    worker.archive_state.mkdir(exist_ok=True)
    atomic_json(
        worker.archive_state / "blk00000.dat.json",
        {**sidecar, "destination": uri, "endpoint": worker.c["endpoint"], "mtime_ns": MTIME},
    )
    directory = archive_directory(worker, tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    if packed:
        packed_bytes = subprocess.run(["zstd", "-q", "-c"], input=PAYLOAD, check=True, stdout=subprocess.PIPE).stdout
        (directory / "blk00000.dat.zst").write_bytes(packed_bytes)
    else:
        (directory / "blk00000.dat").write_bytes(PAYLOAD)
    (directory / "blk00000.dat.json").write_text(json.dumps(sidecar))


def synchronized_and_pruned(worker, tmp_path, monkeypatch, events, packed=True):
    """One finished synchronization whose checkpoint pruned the archived blk00000.dat."""
    fake_components(worker, monkeypatch, events)
    monkeypatch.setattr(worker, "reindex_core", lambda: events.append("reindex"))
    synchronize(worker, "restore", "producer")
    archive_oldest_file(worker, tmp_path, packed)
    synchronize(worker, "blocksci", "checkpoint")
    assert not (worker.core_dir / "blocks/blk00000.dat").exists()


def delete_derived_backups(tmp_path):
    """What the Lab's mainnet S3 deletion of BlockSci, Dumplings and Explorer leaves."""
    shutil.rmtree(tmp_path / "s3/local/dumps")
    shutil.rmtree(tmp_path / "s3/local/index")


def test_an_empty_dataset_is_rebuilt_from_the_checkpoint_and_the_block_archive(worker, tmp_path, monkeypatch):
    events = []
    synchronized_and_pruned(worker, tmp_path, monkeypatch, events)
    delete_derived_backups(tmp_path)
    wipe_working_dataset(worker)
    worker.run_id = "mainnet-sync-local-2"
    synchronize(worker, "restore")
    restore = json.loads((worker.run / "restore.json").read_text())
    assert restore["action"] == "rebuilt-from-archive"
    assert (restore["rebuilt"], set(restore["parts"]), set(restore["backups"])) == (
        ["dumplings", "index"],
        {"core"},
        {"core"},
    )
    assert (restore["blocks"]["files"], restore["blocks"]["bytes"]) == (1, len(PAYLOAD))
    block = worker.core_dir / "blocks/blk00000.dat"
    # Byte for byte the archived file, with the mtime its archive marker recorded.
    assert block.read_bytes() == PAYLOAD and block.stat().st_mtime_ns == MTIME
    assert (worker.core_dir / "chainstate/state.dat").read_text() == "verified chainstate"
    assert worker.reindex_marker.is_file()
    assert not [*worker.root.glob(".restore-*"), *worker.index.glob(".restore-*")]

    # The producer reindexes Core once, offline, before it starts it with the network.
    events.clear()
    synchronize(worker, "producer")
    assert events[:2] == ["reindex", "online-core"] and not worker.reindex_marker.exists()
    synchronize(worker, "blocksci", "checkpoint")
    assert "blocksci-config" in events
    assert json.loads((worker.run / "done.json").read_text())["run_id"] == "mainnet-sync-local-2"
    assert (tmp_path / "s3/local/dumps/manifest.json").is_file()
    assert (tmp_path / "s3/local/index/manifest.json").is_file()


def test_deleted_parts_beside_core_are_rebuilt_from_raw_archived_files(worker, tmp_path, monkeypatch):
    synchronized_and_pruned(worker, tmp_path, monkeypatch, [], packed=False)
    delete_derived_backups(tmp_path)
    shutil.rmtree(worker.dumps)
    shutil.rmtree(worker.index)
    chainstate = (worker.core_dir / "chainstate/state.dat").stat().st_mtime_ns
    worker.run_id = "mainnet-sync-local-2"
    synchronize(worker, "restore")
    restore = json.loads((worker.run / "restore.json").read_text())
    assert (restore["action"], restore["rebuilt"], restore["parts"]) == (
        "rebuilt-from-archive",
        ["dumplings", "index"],
        {},
    )
    assert (worker.core_dir / "blocks/blk00000.dat").stat().st_mtime_ns == MTIME
    # Core is not downloaded again.
    assert (worker.core_dir / "chainstate/state.dat").stat().st_mtime_ns == chainstate
    worker.stage = "producer"
    mainnet_jobs.require_complete_dataset(worker)


def test_only_the_deleted_part_is_rebuilt_while_the_other_backup_is_restored(worker, tmp_path, monkeypatch):
    synchronized_and_pruned(worker, tmp_path, monkeypatch, [])
    index_files = (worker.index / "files.txt").read_text()
    shutil.rmtree(tmp_path / "s3/local/dumps")
    wipe_working_dataset(worker)
    worker.run_id = "mainnet-sync-local-2"
    synchronize(worker, "restore")
    restore = json.loads((worker.run / "restore.json").read_text())
    assert (restore["action"], restore["rebuilt"], set(restore["parts"])) == (
        "rebuilt-from-archive",
        ["dumplings"],
        {"core", "index"},
    )
    assert (worker.index / "files.txt").read_text() == index_files
    worker.stage = "producer"
    mainnet_jobs.require_complete_dataset(worker)


def test_without_the_archived_file_the_dataset_stays_empty(worker, tmp_path, monkeypatch):
    synchronized_and_pruned(worker, tmp_path, monkeypatch, [])
    delete_derived_backups(tmp_path)
    wipe_working_dataset(worker)
    shutil.rmtree(archive_directory(worker, tmp_path))
    worker.run_id = "mainnet-sync-local-2"
    worker.stage = "restore"
    # The marker is in the checkpoint, the object is not: nothing is restored at all.
    with pytest.raises(ValueError, match="block archive cannot replace them: The block archive lacks blk00000.dat"):
        mainnet_jobs.restore(worker)
    assert worker.dataset_members() == [] and not worker.reindex_marker.exists()


def test_a_history_with_a_gap_is_not_rebuilt(worker, tmp_path, monkeypatch):
    synchronized_and_pruned(worker, tmp_path, monkeypatch, [])
    delete_derived_backups(tmp_path)
    wipe_working_dataset(worker)
    # A checkpoint whose archive-state never recorded blk00000.dat.
    core = tmp_path / "s3/local/core/manifest.json"
    worker.storage.restore_backup("s3://local/core/manifest.json", tmp_path / "checkpoint")
    assert core.is_file()
    (tmp_path / "checkpoint/archive-state/blk00000.dat.json").unlink()
    worker.backup(
        tmp_path / "checkpoint",
        ["bitcoin/blocks", "bitcoin/chainstate", "archive-state"],
        worker.c["backup_uri"],
        json.loads(core.read_text()),
        "checkpoint.tar.zst",
    )
    worker.stage = "restore"
    with pytest.raises(ValueError, match="block archive cannot replace them: .*leave gaps"):
        mainnet_jobs.restore(worker)
    assert worker.dataset_members() == []


def test_a_damaged_archived_file_never_takes_its_place(worker, tmp_path, monkeypatch):
    synchronized_and_pruned(worker, tmp_path, monkeypatch, [], packed=False)
    delete_derived_backups(tmp_path)
    shutil.rmtree(worker.dumps)
    (archive_directory(worker, tmp_path) / "blk00000.dat").write_bytes(b"x" * len(PAYLOAD))
    worker.stage = "restore"
    with pytest.raises(ValueError, match="does not match its marker"):
        mainnet_jobs.restore(worker)
    assert not (worker.core_dir / "blocks/blk00000.dat").exists()
    assert not [*worker.root.glob(".restore-*")]


def test_the_rebuild_needs_room_for_the_blocks_and_their_undo_data(worker, tmp_path, monkeypatch):
    synchronized_and_pruned(worker, tmp_path, monkeypatch, [])
    delete_derived_backups(tmp_path)
    shutil.rmtree(worker.dumps)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(mainnet_jobs.shutil, "disk_usage", lambda path: usage(0, 0, len(PAYLOAD)))
    worker.stage = "restore"
    with pytest.raises(ValueError, match="with Core's undo data"):
        mainnet_jobs.restore(worker)
    assert not worker.reindex_marker.exists()


def test_reindex_runs_offline_until_core_exits_and_fails_on_an_unclean_exit(worker, monkeypatch):
    commands = []

    def launch(command):
        commands.append(command)
        return subprocess.Popen(["sh", "-c", "exit 3"])

    def no_rpc(method):
        raise subprocess.CalledProcessError(1, method)

    monkeypatch.setattr(worker, "container_command", lambda *args, **kwargs: [str(arg) for arg in args])
    monkeypatch.setattr(worker, "launch_core", launch)
    monkeypatch.setattr(worker, "rpc_info", no_rpc)
    worker.stage = "producer"
    with pytest.raises(RuntimeError, match="Core reindex failed"):
        worker.reindex_core()
    assert {"-reindex", "-stopafterblockimport", "-networkactive=0", "-prune=1"} <= set(commands[0])
    assert worker.core is None


def test_reindex_refuses_a_gap_that_core_would_delete_everything_after(worker, monkeypatch):
    blocks = worker.core_dir / "blocks"
    blocks.mkdir(parents=True, exist_ok=True)
    for name in ("blk00000.dat", "blk00002.dat", "blk00003.dat"):
        (blocks / name).write_bytes(PAYLOAD)
    monkeypatch.setattr(worker, "launch_core", lambda command: pytest.fail("Core must not start"))
    with pytest.raises(ValueError, match="blk00001.dat is missing"):
        worker.reindex_core()
    assert sorted(path.name for path in blocks.glob("blk*.dat")) == ["blk00000.dat", "blk00002.dat", "blk00003.dat"]
