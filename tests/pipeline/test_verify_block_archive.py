"""The embedded block-archive verifier against bitcoin-block-archive sidecars."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coinjoin_pipeline.paths import EXECUTION_ROOT

VERIFIER = EXECUTION_ROOT / "verify_block_archive.py"


def archive(directory: Path, files: dict[str, tuple[bytes, list[list[int]]]]) -> Path:
    """Write block files with the sidecars bitcoin-block-archive uploads next to them."""
    directory.mkdir()
    for name, (payload, ranges) in files.items():
        (directory / name).write_bytes(payload)
        sidecar = {
            "schema_version": 1,
            "file": name,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "height_ranges": ranges,
        }
        (directory / f"{name}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return directory


def verify(blocks: Path, max_block: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(blocks), str(max_block)],
        capture_output=True,
        text=True,
        check=False,
    )


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
    ],
)
def test_incomplete_archives_are_rejected(tmp_path: Path, files, max_block: int, message: str) -> None:
    result = verify(archive(tmp_path / "blocks", files), max_block)
    assert result.returncode != 0
    assert message in result.stderr


def test_a_changed_block_file_is_rejected(tmp_path: Path) -> None:
    blocks = archive(tmp_path / "blocks", {"blk00000.dat": (b"a", [[0, 1]])})
    (blocks / "blk00000.dat").write_bytes(b"z")
    result = verify(blocks, 1)
    assert "checksum mismatch: blk00000.dat" in result.stderr


def test_the_removed_global_manifest_alone_is_not_an_archive(tmp_path: Path) -> None:
    blocks = tmp_path / "blocks"
    blocks.mkdir()
    (blocks / "archive-manifest.json").write_text("{}", encoding="utf-8")
    assert "no blkNNNNN.dat.json sidecars" in verify(blocks, 0).stderr
