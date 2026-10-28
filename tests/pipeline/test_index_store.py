"""The persistent BlockSci index lock, backup and restore behave as the PBS jobs rely on (index_store.sh)."""

import os
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ARCHIVE = Path(__file__).resolve().parents[2] / "src/coinjoin_pipeline/execution/pbs/templates/index_archive.sh"
STORE = Path(__file__).resolve().parents[2] / "src/coinjoin_pipeline/execution/pbs/templates/index_store.sh"
FAKE_S5CMD = Path(__file__).resolve().parents[1] / "support/persistent-index"
BACKUP = "s3://bucket/blocksci-index"

pytestmark = pytest.mark.skipif(
    shutil.which("zstd") is None or shutil.which("mkfifo") is None, reason="index_store.sh needs zstd and mkfifo"
)


def shell_script(tmp_path, body, *, index="index", backup=BACKUP, expires_in=3600, wait=2, job="job-1"):
    """A bash script running `body` with the store functions loaded, as one PBS job would."""
    return f"""set -euo pipefail
export PATH={FAKE_S5CMD}:$PATH FAKE_S3={tmp_path}/s3
S3_CREDENTIALS_FILE=/dev/null S3_PROFILE=test S3_ENDPOINT_URL=https://s3.example.invalid
RUN_WORK={tmp_path}/run-{job}
mkdir -p "$RUN_WORK"
INDEX_DIR={tmp_path}/{index}
INDEX_BACKUP={backup or ""}
INDEX_LOCK_EXPIRES=$(($(date +%s) + {expires_in}))
INDEX_LOCK_WAIT={wait}
INDEX_LOCK_POLL=0.1
PBS_JOBID={job}
RUN_ID={job}
# The stage header's s5 wrapper (templates/s3_stage.sh).
s5() {{ s5cmd --credentials-file "$S3_CREDENTIALS_FILE" --profile "$S3_PROFILE" --endpoint-url "$S3_ENDPOINT_URL" "$@"; }}
. {ARCHIVE}
. {STORE}
mkdir -p "$INDEX_DIR"
{body}
"""


def shell(tmp_path, body, *, check=True, env=None, **settings):
    return subprocess.run(
        ["bash", "-c", shell_script(tmp_path, body, **settings)],
        text=True,
        capture_output=True,
        check=check,
        env={**os.environ, **(env or {})},
    )


def parse_index(tmp_path, height=100, job="job-1"):
    """Stand in for a parse: write files, then record the index as the template does."""
    return shell(
        tmp_path,
        f"""index_lock_exclusive
touch "$INDEX_DIR/.writing"
index_link_into_run
mkdir -p "$RUN_WORK/blocksci_data/parsed/chain"
printf 'blocks-{height}' > "$RUN_WORK/blocksci_data/parsed/chain/block.dat"
printf '{{"dataDirectory": "x"}}' > "$RUN_WORK/blocksci_data/config.json"
printf '{{"exported_max_block": {height}}}' > "$RUN_WORK/manifest.json"
index_publish "$RUN_WORK/manifest.json"
index_unlock""",
        job=job,
    )


def backup(tmp_path, job="backup", **settings):
    return shell(tmp_path, "index_lock_shared\nindex_backup\nindex_unlock", job=job, **settings)


def restore(tmp_path, **settings):
    return shell(tmp_path, "index_lock_exclusive\nindex_reconcile", job="update", **settings)


def s3(tmp_path):
    return tmp_path / "s3/bucket/blocksci-index"


def block(tmp_path):
    return (tmp_path / "index/parsed/chain/block.dat").read_text()


def test_published_index_is_complete(tmp_path):
    parse_index(tmp_path)

    result = shell(tmp_path, 'index_complete "$INDEX_DIR" && index_height "$INDEX_DIR"')

    assert result.stdout.strip().endswith("100")
    assert block(tmp_path) == "blocks-100"


def test_backup_writes_one_archive_with_its_manifest(tmp_path):
    parse_index(tmp_path)

    backup(tmp_path, env={"FAKE_S5CMD_FORBID_ARCHIVE_CAT": "1"})

    assert sorted(path.name for path in (s3(tmp_path) / "generations/backup").iterdir()) == [
        "blocksci_index.tar.zst",
        "blocksci_index.tar.zst.sha256",
        "manifest.json",
    ]
    assert '"exported_max_block": 100' in (s3(tmp_path) / "generations/backup/manifest.json").read_text()
    assert json.loads((s3(tmp_path) / "latest.json").read_text())["prefix"].endswith("/generations/backup")
    pointer = json.loads((s3(tmp_path) / "latest.json").read_text())
    assert pointer["archive_size"] == (s3(tmp_path) / "generations/backup/blocksci_index.tar.zst").stat().st_size
    assert pointer["upload_validation"] == "size-only"
    assert not (tmp_path / "index/.lock/readers/backup").exists()


def test_an_interrupted_update_is_restored_from_the_backup(tmp_path):
    parse_index(tmp_path)
    backup(tmp_path)
    # An update died midway: its marker stays and a file changed.
    (tmp_path / "index/.writing").touch()
    (tmp_path / "index/parsed/chain/block.dat").write_text("half-written")

    result = restore(tmp_path)

    assert "restoring the working copy" in result.stdout
    assert block(tmp_path) == "blocks-100"


def test_a_lost_working_copy_is_restored_from_the_newest_backup(tmp_path):
    # Multijob scratch may purge files, or the whole directory.
    parse_index(tmp_path, height=100)
    backup(tmp_path)
    parse_index(tmp_path, height=200)
    backup(tmp_path, job="backup-2")
    shutil.rmtree(tmp_path / "index")

    restore(tmp_path)

    assert block(tmp_path) == "blocks-200"
    assert len(list((s3(tmp_path) / "generations").iterdir())) == 2


def test_a_broken_upload_preserves_the_previous_verified_backup(tmp_path):
    parse_index(tmp_path)
    backup(tmp_path)

    pointer = (s3(tmp_path) / "latest.json").read_text()
    failed = backup(tmp_path, job="backup-2", check=False, env={"FAKE_S5CMD_FAIL_PIPE": "1"})

    assert failed.returncode != 0
    assert (s3(tmp_path) / "latest.json").read_text() == pointer
    assert (s3(tmp_path) / "generations/backup/manifest.json").exists()
    shutil.rmtree(tmp_path / "index")
    restore(tmp_path)
    assert block(tmp_path) == "blocks-100"


def test_without_s5cmd_pipe_the_archive_passes_through_scratch(tmp_path):
    parse_index(tmp_path)

    backup(tmp_path, env={"FAKE_S5CMD_NO_PIPE": "1"})
    shutil.rmtree(tmp_path / "index")
    restore(tmp_path)

    assert block(tmp_path) == "blocks-100"


def test_a_remote_size_mismatch_does_not_replace_the_previous_backup(tmp_path):
    parse_index(tmp_path)
    backup(tmp_path)
    pointer = (s3(tmp_path) / "latest.json").read_bytes()
    failed = backup(tmp_path, job="backup-2", check=False, env={
        "FAKE_S5CMD_LS_SIZE_DELTA": "1", "FAKE_S5CMD_FORBID_ARCHIVE_CAT": "1",
    })
    assert failed.returncode != 0
    assert "size/key mismatch" in failed.stderr
    assert (s3(tmp_path) / "latest.json").read_bytes() == pointer
    assert not (s3(tmp_path) / "generations/backup-2/manifest.json").exists()


def test_a_corrupted_backup_is_refused(tmp_path):
    parse_index(tmp_path)
    backup(tmp_path)
    pointer = json.loads((s3(tmp_path) / "latest.json").read_text())
    pointer["archive_sha256"] = "0" * 64
    (s3(tmp_path) / "latest.json").write_text(json.dumps(pointer))
    shutil.rmtree(tmp_path / "index")

    result = restore(tmp_path, check=False)

    assert result.returncode == 1
    assert "does not match its SHA-256" in result.stderr


def test_without_a_backup_an_incomplete_index_stops_the_job(tmp_path):
    parse_index(tmp_path)
    (tmp_path / "index/.writing").touch()

    result = restore(tmp_path, backup=None, check=False)

    assert result.returncode == 1
    assert "there is no S3 backup" in result.stderr


def test_readers_share_and_a_writer_waits_for_them(tmp_path):
    shell(tmp_path, "index_lock_shared", job="reader-1")
    shell(tmp_path, "index_lock_shared", job="reader-2")

    blocked = shell(tmp_path, "index_lock_exclusive", job="writer", check=False)

    assert blocked.returncode == 1
    assert "readers still hold" in blocked.stderr
    # The writer that gave up leaves no entry behind, so readers keep working.
    assert not (tmp_path / "index/.lock/writer").exists()
    assert sorted(p.name for p in (tmp_path / "index/.lock/readers").iterdir()) == ["reader-1", "reader-2"]


def test_a_reader_waits_while_the_writer_holds_the_index(tmp_path):
    shell(tmp_path, "index_lock_exclusive", job="writer")

    blocked = shell(tmp_path, "index_lock_shared", job="reader", check=False)

    assert blocked.returncode == 1
    assert "is being updated" in blocked.stderr
    assert not (tmp_path / "index/.lock/readers/reader").exists()


def test_a_lock_left_by_a_killed_job_expires(tmp_path):
    shell(tmp_path, "index_lock_exclusive", job="killed", expires_in=1)
    time.sleep(1.2)

    result = shell(tmp_path, "index_lock_shared && echo reading", job="reader")

    assert "clearing expired lock writer of job killed" in result.stdout
    assert result.stdout.strip().endswith("reading")


def test_a_purged_file_makes_the_working_copy_incomplete(tmp_path):
    parse_index(tmp_path)
    (tmp_path / "index/parsed/chain/block.dat").unlink()

    result = shell(tmp_path, 'index_complete "$INDEX_DIR" || echo incomplete')

    assert result.stdout.strip() == "incomplete"


def test_a_writer_proceeds_once_the_readers_finish(tmp_path):
    readers = [
        subprocess.Popen(
            ["bash", "-c", shell_script(tmp_path, "index_lock_shared\nsleep 1\nindex_unlock", job=f"reader-{n}")]
        )
        for n in (1, 2)
    ]
    time.sleep(0.5)
    started = time.monotonic()

    shell(tmp_path, "index_lock_exclusive\necho writing", job="writer", wait=10)

    assert time.monotonic() - started >= 0.3
    assert [reader.wait() for reader in readers] == [0, 0]
