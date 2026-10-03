"""The embedded block-archive helper and the PBS shell that drives it."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coinjoin_pipeline.execution.pbs.templates_s3 import _download_block_archive
from coinjoin_pipeline.paths import EXECUTION_ROOT

HELPER = EXECUTION_ROOT / "block_archive.py"
Files = dict[str, tuple[bytes, list[list[int]]]]


def sidecar(name: str, payload: bytes, ranges: list[list[int]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "file": name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "height_ranges": ranges,
    }


def archive(directory: Path, files: Files, *, sidecars_only: bool = False) -> Path:
    """Write block files with the sidecars bitcoin-block-archive uploads next to them."""
    directory.mkdir(parents=True)
    for name, (payload, ranges) in files.items():
        if not sidecars_only:
            (directory / name).write_bytes(payload)
        (directory / f"{name}.json").write_text(json.dumps(sidecar(name, payload, ranges)), encoding="utf-8")
    return directory


def index_sha256(files: Files, last: int) -> str:
    digest = hashlib.sha256()
    for name, (payload, _) in list(files.items())[: last + 1]:
        digest.update(f"{name} {hashlib.sha256(payload).hexdigest()}\n".encode("ascii"))
    return digest.hexdigest()


def source_manifest(path: Path, files: Files, *, max_block: int, last_file: int) -> Path:
    path.write_text(
        json.dumps(
            {
                "source_kind": "bitcoin-blocks-s3",
                "exported_max_block": max_block,
                "exported_block_hash": "a" * 64,
                "block_archive_last_file": last_file,
                "block_archive_index_sha256": index_sha256(files, last_file),
            }
        ),
        encoding="utf-8",
    )
    return path


def helper(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
    )


def select(tmp_path: Path, files: Files, max_block: int, manifest: Path | None = None) -> list[str]:
    """Names the helper would download; raises with its message on refusal."""
    sidecars = archive(tmp_path / "sidecars", files, sidecars_only=True)
    blocks = tmp_path / "blocks"
    blocks.mkdir()
    extra = ["--source-manifest", manifest] if manifest is not None else []
    result = helper(
        "select",
        sidecars,
        blocks,
        "--uri",
        "s3://bucket/blocks",
        "--max-block",
        max_block,
        *extra,
        "--commands",
        tmp_path / "download.s5cmd",
        "--manifest-fields",
        tmp_path / "fields.json",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    lines = (tmp_path / "download.s5cmd").read_text(encoding="utf-8").splitlines()
    names = [line.split()[1].rsplit("/", 1)[1] for line in lines]
    assert lines == [f"cp s3://bucket/blocks/{name} {blocks}/" for name in names]
    assert sorted(path.name for path in blocks.iterdir()) == sorted(f"{name}.json" for name in names)
    return names


def verify(blocks: Path, max_block: int, manifest: Path | None = None) -> subprocess.CompletedProcess[str]:
    extra = ["--source-manifest", manifest] if manifest is not None else []
    return helper("verify", blocks, "--max-block", max_block, *extra)


TEN_PER_FILE: Files = {
    f"blk{number:05d}.dat": (bytes([number]), [[number * 10, number * 10 + 9]]) for number in range(5)
}


def test_contiguous_archive_covering_the_requested_height_passes(tmp_path: Path) -> None:
    blocks = archive(tmp_path / "blocks", {"blk00000.dat": (b"a", [[0, 4]]), "blk00001.dat": (b"b", [[5, 9]])})
    assert verify(blocks, 9).returncode == 0


@pytest.mark.parametrize(
    ("files", "max_block", "message"),
    [
        ({"blk00000.dat": (b"a", [[0, 4]]), "blk00002.dat": (b"c", [[5, 9]])}, 9, "gap before blk00002.dat.json"),
        ({"blk00000.dat": (b"a", [[0, 4]]), "blk00001.dat": (b"b", [[6, 9]])}, 9, "height 5 is missing"),
        ({"blk00000.dat": (b"a", [[0, 4]])}, 5, "height 5 is missing"),
        ({"blk00000.dat": (b"a", [[0, -1]])}, 0, "invalid height ranges"),
        ({"blk00001.dat": (b"b", [[0, 4]])}, 0, "gap before blk00001.dat.json"),
    ],
)
def test_incomplete_archives_are_rejected(tmp_path: Path, files: Files, max_block: int, message: str) -> None:
    result = verify(archive(tmp_path / "blocks", files), max_block)
    assert result.returncode != 0
    assert message in result.stderr


def test_a_changed_block_file_is_rejected(tmp_path: Path) -> None:
    blocks = archive(tmp_path / "blocks", {"blk00000.dat": (b"a", [[0, 1]])})
    (blocks / "blk00000.dat").write_bytes(b"zz")
    result = verify(blocks, 1)
    assert "missing or changed: blk00000.dat" in result.stderr

    (blocks / "blk00000.dat").write_bytes(b"b")
    assert "checksum mismatch: blk00000.dat" in verify(blocks, 1).stderr


def test_the_removed_global_manifest_alone_is_not_an_archive(tmp_path: Path) -> None:
    blocks = tmp_path / "blocks"
    blocks.mkdir()
    (blocks / "archive-manifest.json").write_text("{}", encoding="utf-8")
    assert "no blkNNNNN.dat.json sidecars" in verify(blocks, 0).stderr


def test_parse_downloads_only_the_prefix_up_to_the_tip_margin(tmp_path: Path) -> None:
    # Heights up to 12 + 6 sit in the first two files; the rest of the archive is not needed.
    assert select(tmp_path, TEN_PER_FILE, 12) == ["blk00000.dat", "blk00001.dat"]
    fields = json.loads("{" + (tmp_path / "fields.json").read_text(encoding="utf-8").lstrip(",") + "}")
    assert fields == {"block_archive_last_file": 1, "block_archive_index_sha256": index_sha256(TEN_PER_FILE, 1)}


def test_parse_keeps_files_whose_first_block_is_low_enough(tmp_path: Path) -> None:
    # Blocks arrive out of order during IBD: blk00002 still starts below the limit.
    files: Files = {
        "blk00000.dat": (b"a", [[0, 9]]),
        "blk00001.dat": (b"b", [[10, 19], [21, 21]]),
        "blk00002.dat": (b"c", [[20, 20], [22, 29]]),
        "blk00003.dat": (b"d", [[30, 39]]),
    }
    assert select(tmp_path, files, 14) == ["blk00000.dat", "blk00001.dat", "blk00002.dat"]


def test_parse_of_the_archive_tip_takes_every_file(tmp_path: Path) -> None:
    assert select(tmp_path, TEN_PER_FILE, 49) == list(TEN_PER_FILE)


def test_update_resumes_at_the_last_scanned_file_and_fetches_older_new_heights(tmp_path: Path) -> None:
    files: Files = {
        "blk00000.dat": (b"a", [[0, 8], [13, 13]]),
        "blk00001.dat": (b"b", [[9, 12], [14, 19]]),
        "blk00002.dat": (b"c", [[20, 29]]),
        "blk00003.dat": (b"d", [[30, 39]]),
        "blk00004.dat": (b"e", [[40, 49]]),
        "blk00005.dat": (b"f", [[50, 59]]),
    }
    manifest = source_manifest(tmp_path / "manifest.json", files, max_block=12, last_file=1)
    assert select(tmp_path, files, 33, manifest) == ["blk00000.dat", "blk00001.dat", "blk00002.dat", "blk00003.dat"]


def test_update_skips_older_files_without_new_heights(tmp_path: Path) -> None:
    manifest = source_manifest(tmp_path / "manifest.json", TEN_PER_FILE, max_block=25, last_file=3)
    assert select(tmp_path, TEN_PER_FILE, 44, manifest) == ["blk00002.dat", "blk00003.dat", "blk00004.dat"]


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ({"source_kind": "external-bitcoin"}, "requires a bitcoin-blocks-s3 cache"),
        ({"source_kind": "bitcoin-blocks-s3", "exported_max_block": 12}, "parse it again"),
        (
            {
                "source_kind": "bitcoin-blocks-s3",
                "exported_max_block": 12,
                "exported_block_hash": "a" * 64,
                "block_archive_last_file": 1,
                "block_archive_index_sha256": "0" * 64,
            },
            "files indexed by the source cache have changed",
        ),
        (
            {
                "source_kind": "bitcoin-blocks-s3",
                "exported_max_block": 12,
                "exported_block_hash": "a" * 64,
                "block_archive_last_file": 9,
                "block_archive_index_sha256": "0" * 64,
            },
            "files indexed by the source cache have changed",
        ),
        (
            {
                "source_kind": "bitcoin-blocks-s3",
                "exported_max_block": 40,
                "exported_block_hash": "a" * 64,
                "block_archive_last_file": 1,
                "block_archive_index_sha256": index_sha256(TEN_PER_FILE, 1),
            },
            "must be greater than source 40",
        ),
    ],
)
def test_update_refuses_a_cache_it_cannot_resume(tmp_path: Path, manifest: dict[str, object], message: str) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError, match=message):
        select(tmp_path, TEN_PER_FILE, 33, path)


def test_update_rejects_old_cache_without_verified_tip_before_downloading_blocks(tmp_path: Path) -> None:
    manifest = source_manifest(tmp_path / "manifest.json", TEN_PER_FILE, max_block=12, last_file=1)
    value = json.loads(manifest.read_text())
    del value["exported_block_hash"]
    manifest.write_text(json.dumps(value))
    with pytest.raises(AssertionError, match="lacks a verified block hash; parse it again"):
        select(tmp_path, TEN_PER_FILE, 33, manifest)
    assert not (tmp_path / "download.s5cmd").exists()


def test_select_rejects_a_gap_in_the_archive(tmp_path: Path) -> None:
    files = {name: value for name, value in TEN_PER_FILE.items() if name != "blk00002.dat"}
    with pytest.raises(AssertionError, match="gap before blk00003.dat.json"):
        select(tmp_path, files, 12)


def test_update_verification_needs_new_heights_and_the_resume_file(tmp_path: Path) -> None:
    manifest = source_manifest(tmp_path / "manifest.json", TEN_PER_FILE, max_block=25, last_file=3)
    present = {name: TEN_PER_FILE[name] for name in ("blk00002.dat", "blk00003.dat", "blk00004.dat")}
    blocks = archive(tmp_path / "blocks", present)
    assert verify(blocks, 44, manifest).returncode == 0
    assert "heights 26..50; height 50 is missing" in verify(blocks, 50, manifest).stderr

    (blocks / "blk00003.dat").unlink()
    (blocks / "blk00003.dat.json").unlink()
    assert "gap before blk00004.dat.json" in verify(blocks, 44, manifest).stderr


FAKE_S5CMD = r"""#!/usr/bin/env python3
import glob, os, shlex, shutil, sys

root = os.environ["FAKE_S3_ROOT"]
arguments = sys.argv[1:]
while arguments[0].startswith("--"):
    arguments = arguments[2:]

def local(uri):
    return os.path.join(root, uri[len("s3://"):])

def copy(source, destination):
    sources = sorted(glob.glob(local(source))) if "*" in source else [local(source)]
    if not sources or not all(os.path.isfile(path) for path in sources):
        sys.exit(f"no object found: {source}")
    for path in sources:
        shutil.copy(path, destination)
        with open(os.environ["FAKE_S3_LOG"], "a") as log:
            log.write(os.path.basename(path) + "\n")

if arguments[0] == "cp":
    copy(*arguments[1:])
elif arguments[0] == "run":
    for line in open(arguments[1]):
        command, *rest = shlex.split(line)
        assert command == "cp"
        copy(*rest)
else:
    sys.exit(f"unsupported fake s5cmd command: {arguments}")
"""


def run_download(tmp_path: Path, max_block: int, manifest: Path | None = None) -> tuple[list[str], str]:
    """Execute the PBS download fragment against a directory-backed fake S3."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "s5cmd").write_text(FAKE_S5CMD, encoding="utf-8")
    (fake_bin / "s5cmd").chmod(0o755)
    log = tmp_path / "downloads.log"
    log.unlink(missing_ok=True)
    work = tmp_path / f"work-{max_block}"
    script = (
        "set -euo pipefail\n"
        f'RUN_WORK="{work}"\n'
        "BITCOIN_BLOCKS_URI=s3://bucket/blocks\n"
        f"EXPORTED_MAX_BLOCK={max_block}\n"
        f'SOURCE_CACHE_DIR="{manifest.parent if manifest else ""}"\n'
        f"{_download_block_archive('$SOURCE_CACHE_DIR/manifest.json' if manifest else None)}\n"
        f'printf "%s" "$MANIFEST_EXTRA" >"{work}/fields"\n'
    )
    result = subprocess.run(
        ["bash"],
        input=script,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_S3_ROOT": str(tmp_path / "s3"),
            "FAKE_S3_LOG": str(log),
            "S3_CREDENTIALS_FILE": "unused",
            "S3_PROFILE": "unused",
            "S3_ENDPOINT_URL": "http://unused",
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr
    downloaded = [name for name in log.read_text(encoding="utf-8").splitlines() if not name.endswith(".json")]
    return downloaded, (work / "fields").read_text(encoding="utf-8")


def test_pbs_fragment_parses_then_updates_from_the_archive(tmp_path: Path) -> None:
    files = dict(list(TEN_PER_FILE.items())[:3])
    archive(tmp_path / "s3" / "bucket" / "blocks", files)

    downloaded, fields = run_download(tmp_path, 12)
    assert downloaded == ["blk00000.dat", "blk00001.dat"]
    parse_manifest = json.loads('{"source_kind": "bitcoin-blocks-s3", "exported_max_block": 12' + fields + "}")
    # The post-parser verification, separate from this download fragment, adds this field.
    parse_manifest["exported_block_hash"] = "a" * 64

    # The archive grows; the update reuses the parse manifest exactly as the cache stores it.
    archive_dir = tmp_path / "s3" / "bucket" / "blocks"
    for name, (payload, ranges) in list(TEN_PER_FILE.items())[3:]:
        (archive_dir / name).write_bytes(payload)
        (archive_dir / f"{name}.json").write_text(json.dumps(sidecar(name, payload, ranges)), encoding="utf-8")
    cache = tmp_path / "source-cache"
    cache.mkdir()
    (cache / "manifest.json").write_text(json.dumps(parse_manifest), encoding="utf-8")

    downloaded, fields = run_download(tmp_path, 44, cache / "manifest.json")
    assert downloaded == ["blk00001.dat", "blk00002.dat", "blk00003.dat", "blk00004.dat"]
    assert '"block_archive_last_file": 4' in fields


def test_pbs_fragment_refuses_an_archive_rewritten_under_the_cache(tmp_path: Path) -> None:
    archive_dir = archive(tmp_path / "s3" / "bucket" / "blocks", TEN_PER_FILE)
    _, fields = run_download(tmp_path, 12)
    cache = tmp_path / "source-cache"
    cache.mkdir()
    manifest = cache / "manifest.json"
    manifest.write_text('{"source_kind": "bitcoin-blocks-s3", "exported_max_block": 12' + fields + "}", "utf-8")
    recorded = json.loads(manifest.read_text())
    recorded["exported_block_hash"] = "a" * 64
    manifest.write_text(json.dumps(recorded))

    (archive_dir / "blk00000.dat").write_bytes(b"other node")
    (archive_dir / "blk00000.dat.json").write_text(
        json.dumps(sidecar("blk00000.dat", b"other node", [[0, 9]])), encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="have changed"):
        run_download(tmp_path, 44, manifest)
