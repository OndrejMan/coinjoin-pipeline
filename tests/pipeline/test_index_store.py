"""The persistent BlockSci index lock, backup and restore behave as the PBS jobs rely on (index_store.py)."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from coinjoin_pipeline.execution.pbs.index_store import IndexStore, IndexStoreError
from coinjoin_pipeline.execution.pbs.stage_runtime import kept_index, report_kept_copy, scratch_index
from coinjoin_pipeline.storage.s5client import S5Client, S5Error

ROOT = Path(__file__).resolve().parents[2]
FAKE_S5CMD = ROOT / "tests/support/persistent-index"
BACKUP = "s3://bucket/blocksci-index"

pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="the index archive needs zstd")


@pytest.fixture(autouse=True)
def fake_s3(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE_S5CMD}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_S3", str(tmp_path / "s3"))


def store(tmp_path, *, job="job-1", index="index", backup=BACKUP, lock_seconds=3600, wait=2, env=None):
    """One job's view of the index, as a PBS job builds it from its settings."""
    if env:
        os.environ.update(env)
    try:
        storage = S5Client("https://s3.example.invalid", "/dev/null", "test")
    finally:
        for name in env or {}:
            del os.environ[name]
    return IndexStore(
        str(tmp_path / index),
        backup,
        storage=storage,
        job_id=job,
        lock_seconds=lock_seconds,
        wait_seconds=wait,
        poll_seconds=0.1,
    )


def parse_index(tmp_path, height=100, job="job-1"):
    """Stand in for a parse: write files, then record the index as the parse job does."""
    index = store(tmp_path, job=job)
    index.lock_exclusive()
    index.begin_write()
    run = tmp_path / f"run-{job}"
    index.link_into_run(run)
    (run / "blocksci_data/parsed/chain").mkdir(parents=True, exist_ok=True)
    (run / "blocksci_data/parsed/chain/block.dat").write_text(f"blocks-{height}")
    (run / "blocksci_data/config.json").write_text('{"dataDirectory": "x"}')
    index.publish(run / "blocksci_data/config.json", {"exported_max_block": height})
    index.unlock()


def backup(tmp_path, job="backup", **settings):
    index = store(tmp_path, job=job, **settings)
    index.lock_shared()
    try:
        index.backup(threads=1)
    finally:
        index.unlock()


def restore(tmp_path, **settings):
    index = store(tmp_path, job="update", **settings)
    index.lock_exclusive()
    index.reconcile()


def s3(tmp_path):
    return tmp_path / "s3/bucket/blocksci-index"


def block(tmp_path):
    return (tmp_path / "index/parsed/chain/block.dat").read_text()


def test_published_index_is_complete(tmp_path):
    parse_index(tmp_path)

    index = store(tmp_path)

    assert index.complete() and index.height() == 100
    assert block(tmp_path) == "blocks-100"


def test_backup_writes_one_archive_with_its_manifest(tmp_path):
    parse_index(tmp_path)

    backup(tmp_path, env={"FAKE_S5CMD_FORBID_ARCHIVE_CAT": "1"})

    assert sorted(path.name for path in s3(tmp_path).iterdir()) == [
        "blocksci_index.tar.zst",
        "blocksci_index.tar.zst.sha256",
        "manifest.json",
    ]
    manifest = json.loads((s3(tmp_path) / "manifest.json").read_text())
    assert manifest["exported_max_block"] == 100
    assert manifest["archive_uri"] == BACKUP + "/blocksci_index.tar.zst"
    assert manifest["archive_size"] == (s3(tmp_path) / "blocksci_index.tar.zst").stat().st_size
    assert manifest["upload_validation"] == "size-only"
    assert not (tmp_path / "index/.lock/readers/backup").exists()


def test_an_interrupted_update_is_restored_from_the_backup(tmp_path, capsys):
    parse_index(tmp_path)
    backup(tmp_path)
    # An update died midway: its marker stays and a file changed.
    (tmp_path / "index/.writing").touch()
    (tmp_path / "index/parsed/chain/block.dat").write_text("half-written")

    restore(tmp_path)

    assert "restoring the working copy" in capsys.readouterr().out
    assert block(tmp_path) == "blocks-100"


def test_a_new_backup_replaces_the_previous_one(tmp_path):
    # Multijob scratch may purge files, or the whole directory.
    parse_index(tmp_path, height=100)
    backup(tmp_path)
    parse_index(tmp_path, height=200)
    backup(tmp_path, job="backup-2")
    shutil.rmtree(tmp_path / "index")

    restore(tmp_path)

    assert block(tmp_path) == "blocks-200"
    assert sorted(path.name for path in s3(tmp_path).iterdir()) == [
        "blocksci_index.tar.zst",
        "blocksci_index.tar.zst.sha256",
        "manifest.json",
    ]


def test_a_broken_upload_leaves_no_valid_backup(tmp_path):
    parse_index(tmp_path)
    backup(tmp_path)

    with pytest.raises(S5Error):
        backup(tmp_path, job="backup-2", env={"FAKE_S5CMD_FAIL_PIPE": "1"})

    assert not (s3(tmp_path) / "manifest.json").exists()
    shutil.rmtree(tmp_path / "index")
    with pytest.raises(IndexStoreError, match="there is no S3 backup"):
        restore(tmp_path)


def test_without_s5cmd_pipe_the_archive_passes_through_scratch(tmp_path):
    parse_index(tmp_path)

    backup(tmp_path, env={"FAKE_S5CMD_NO_PIPE": "1"})
    shutil.rmtree(tmp_path / "index")
    restore(tmp_path)

    assert block(tmp_path) == "blocks-100"


def test_a_remote_size_mismatch_publishes_no_manifest(tmp_path):
    parse_index(tmp_path)

    with pytest.raises(IndexStoreError, match="size/key mismatch"):
        backup(tmp_path, env={"FAKE_S5CMD_LS_SIZE_DELTA": "1", "FAKE_S5CMD_FORBID_ARCHIVE_CAT": "1"})

    assert not (s3(tmp_path) / "manifest.json").exists()


def test_a_corrupted_backup_is_refused(tmp_path):
    parse_index(tmp_path)
    backup(tmp_path)
    manifest = json.loads((s3(tmp_path) / "manifest.json").read_text())
    manifest["archive_sha256"] = "0" * 64
    (s3(tmp_path) / "manifest.json").write_text(json.dumps(manifest))
    shutil.rmtree(tmp_path / "index")

    with pytest.raises(S5Error, match="does not match its SHA-256"):
        restore(tmp_path)
    assert (tmp_path / "index/.writing").exists()


def test_without_a_backup_an_incomplete_index_stops_the_job(tmp_path):
    parse_index(tmp_path)
    (tmp_path / "index/.writing").touch()

    with pytest.raises(IndexStoreError, match="there is no S3 backup"):
        restore(tmp_path, backup=None)


def test_readers_share_and_a_writer_waits_for_them(tmp_path):
    store(tmp_path, job="reader-1").lock_shared()
    store(tmp_path, job="reader-2").lock_shared()

    with pytest.raises(IndexStoreError, match="readers still hold"):
        store(tmp_path, job="writer").lock_exclusive()

    # The writer that gave up leaves no entry behind, so readers keep working.
    assert not (tmp_path / "index/.lock/writer").exists()
    assert sorted(p.name for p in (tmp_path / "index/.lock/readers").iterdir()) == ["reader-1", "reader-2"]


def test_a_reader_waits_while_the_writer_holds_the_index(tmp_path):
    store(tmp_path, job="writer").lock_exclusive()

    with pytest.raises(IndexStoreError, match="is being updated"):
        store(tmp_path, job="reader").lock_shared()

    assert not (tmp_path / "index/.lock/readers/reader").exists()


def test_a_lock_left_by_a_killed_job_expires(tmp_path, capsys):
    store(tmp_path, job="killed", lock_seconds=1).lock_exclusive()
    time.sleep(1.2)

    reader = store(tmp_path, job="reader")
    reader.lock_shared()

    assert "clearing expired lock writer of job killed" in capsys.readouterr().out
    assert reader.held == tmp_path / "index/.lock/readers/reader"


def test_a_purged_file_makes_the_working_copy_incomplete(tmp_path):
    parse_index(tmp_path)
    (tmp_path / "index/parsed/chain/block.dat").unlink()

    assert not store(tmp_path).complete()


def hold(tmp_path, job, mode, seconds):
    """Another job, in its own process, that holds the lock for a while."""
    program = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from coinjoin_pipeline.execution.pbs.index_store import IndexStore\n"
        f"store = IndexStore({str(tmp_path / 'index')!r}, None, job_id={job!r}, lock_seconds=3600, wait_seconds=10,"
        " poll_seconds=0.1)\n"
        f"store.lock_{mode}()\n"
        f"time.sleep({seconds})\n"
        "store.unlock()\n"
    )
    return subprocess.Popen([sys.executable, "-c", program])


def test_a_writer_proceeds_once_the_readers_finish(tmp_path):
    readers = [hold(tmp_path, f"reader-{n}", "shared", 1) for n in (1, 2)]
    time.sleep(0.5)
    started = time.monotonic()

    store(tmp_path, job="writer", wait=10).lock_exclusive()

    assert time.monotonic() - started >= 0.3
    assert [reader.wait() for reader in readers] == [0, 0]


def test_an_aliased_index_is_bound_at_its_target_too(tmp_path):
    # A /scratch/multijob-* alias points to another filesystem the container lacks.
    (tmp_path / "beegfs").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "beegfs")
    real = os.path.realpath(tmp_path / "beegfs")

    assert store(tmp_path, index="alias").binds("ro") == [f"{real}:{real}:ro", f"{real}:{tmp_path}/alias:ro"]


def test_a_plain_index_is_bound_once(tmp_path):
    real = str(tmp_path / "index")

    assert store(tmp_path).binds("rw") == [f"{real}:{real}:rw"]


def shell_lock(tmp_path, job, body):
    """The shell library of earlier jobs, which can still run while new ones start."""
    library = subprocess.run(
        ["git", "show", "HEAD:src/coinjoin_pipeline/execution/pbs/templates/index_store.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if library.returncode:
        pytest.skip("the shell index library is no longer in HEAD")
    script = (
        f"set -euo pipefail\nINDEX_DIR={tmp_path}/index\nINDEX_BACKUP=\nPBS_JOBID={job}\n"
        "INDEX_LOCK_EXPIRES=$(($(date +%s) + 3600))\nINDEX_LOCK_WAIT=1\nINDEX_LOCK_POLL=0.1\n"
        f"{library.stdout}\nmkdir -p \"$INDEX_DIR\"\n{body}\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_python_and_shell_jobs_respect_each_others_locks(tmp_path):
    writer = store(tmp_path, job="python-writer")
    writer.lock_exclusive()
    blocked = shell_lock(tmp_path, "shell-reader", "index_lock_shared")
    assert blocked.returncode == 1 and "is being updated" in blocked.stderr
    writer.unlock()

    assert shell_lock(tmp_path, "shell-reader", "index_lock_shared").returncode == 0
    with pytest.raises(IndexStoreError, match="readers still hold"):
        store(tmp_path, job="python-writer", wait=0.5).lock_exclusive()
    assert (tmp_path / "index/.lock/readers/shell-reader/job").read_text() == "shell-reader\n"
    python_entry = store(tmp_path, job="python-reader")
    python_entry.lock_shared()
    assert sorted((tmp_path / "index/.lock/readers/python-reader").iterdir()) == [
        tmp_path / "index/.lock/readers/python-reader/expires",
        tmp_path / "index/.lock/readers/python-reader/job",
    ]


def test_a_reading_job_restores_the_backup_into_its_own_scratch(tmp_path):
    parse_index(tmp_path)
    backup(tmp_path)
    index = store(tmp_path)
    shutil.rmtree(index.path)
    stage = SimpleNamespace(
        settings={"persistent": {"backup_uri": BACKUP}}, scratch=tmp_path / "scratch", storage=index.storage
    )

    copy = scratch_index(stage)

    assert copy.path == tmp_path / "scratch/index"
    assert copy.complete() and copy.height() == 100
    assert not index.path.exists(), "The shared working copy stays untouched"


def keeping_stage(tmp_path, index):
    persistent = {
        "index_dir": str(index.path),
        "backup_uri": BACKUP,
        "lock_seconds": 3600,
        "wait_seconds": 2,
        "poll_seconds": 0.1,
    }
    return SimpleNamespace(settings={"persistent": persistent}, storage=index.storage, finalizers=[])


def test_a_kept_copy_is_filled_once_and_stays_after_the_job(tmp_path):
    parse_index(tmp_path)
    backup(tmp_path)
    index = store(tmp_path)
    shutil.rmtree(index.path)

    kept = kept_index(keeping_stage(tmp_path, index))
    assert kept.complete() and kept.height() == 100 and kept.held is not None
    kept.unlock()
    assert kept.complete(), "The copy stays for the next job"

    (tmp_path / "s3/bucket/blocksci-index/manifest.json").unlink()
    again = kept_index(keeping_stage(tmp_path, index))
    assert again.complete(), "A complete copy needs no backup"
    again.unlock()


def test_a_kept_copy_reports_its_size(tmp_path):
    parse_index(tmp_path)
    index = store(tmp_path)
    stage = SimpleNamespace(storage=index.storage, log=print)

    report_kept_copy(stage, index.path, "s3://bucket/state/notebook-storage.json")

    report = json.loads((tmp_path / "s3/bucket/state/notebook-storage.json").read_text())
    usage = report["directories"][str(index.path)]
    assert report["stage"] == "notebook" and usage["files"] > 0 and usage["bytes"] > 0


def compressed_object(tmp_path, content=b"block data"):
    source = tmp_path / "object"
    source.write_bytes(content)
    (tmp_path / "s3/bucket").mkdir(parents=True, exist_ok=True)
    subprocess.run(["zstd", "-q", str(source), "-o", str(tmp_path / "s3/bucket/object.zst")], check=True)
    return hashlib.sha256((tmp_path / "s3/bucket/object.zst").read_bytes()).hexdigest()


def test_an_unpacked_stream_is_read_and_its_object_checked(tmp_path):
    checksum = compressed_object(tmp_path)
    storage = store(tmp_path).storage

    with storage.unpacked("s3://bucket/object.zst", checksum) as stream:
        assert stream.read() == b"block data"
    with pytest.raises(S5Error, match="does not match its SHA-256"):
        with storage.unpacked("s3://bucket/object.zst", "0" * 64) as stream:
            stream.read()


def test_an_unpacked_stream_of_a_missing_object_fails(tmp_path):
    with pytest.raises(S5Error, match="Reading s3://bucket/missing.zst failed"):
        with store(tmp_path).storage.unpacked("s3://bucket/missing.zst") as stream:
            stream.read()


def test_a_body_leaving_an_unpacked_stream_early_keeps_its_error(tmp_path):
    compressed_object(tmp_path, os.urandom(1 << 20))
    with pytest.raises(LookupError, match="stopped reading"):
        with store(tmp_path).storage.unpacked("s3://bucket/object.zst"):
            raise LookupError("stopped reading")


def test_discard_reads_an_object_and_reports_a_missing_one(tmp_path):
    compressed_object(tmp_path)
    storage = store(tmp_path).storage
    storage.discard("s3://bucket/object.zst")
    with pytest.raises(S5Error, match="Reading s3://bucket/missing failed"):
        storage.discard("s3://bucket/missing")
