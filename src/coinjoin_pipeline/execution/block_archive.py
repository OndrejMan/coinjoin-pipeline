"""Select and verify bitcoin-block-archive files for one BlockSci parse or update.

Every archived ``blkNNNNN.dat`` has a schema-1 sidecar ``blkNNNNN.dat.json``
with its size, SHA-256 and the block heights it holds. A PBS job downloads all
sidecars (a few kB each), lets ``select`` choose the block files BlockSci needs,
downloads only those and runs ``verify`` on them before parsing.

BlockSci scans block files from its starting file upwards, stops at the first
missing number and takes the highest block it has seen as the chain tip:

* a parse through height M uses the prefix blk00000..blkK, where blkK is the
  last file holding a height up to M + TIP_MARGIN, reducing ambiguity near M;
* an update from H to M resumes at the last file of the previous scan, so it
  needs that file onwards (chosen the same way) plus every older file holding
  a height in H+1..M, because BlockSci reads each new block from the file it
  indexed it in. The previous scan's files must be unchanged, which the
  ``block_archive_index_sha256`` recorded in the cache manifest proves.

The script runs on the PBS host's ``python3``, so it stays Python 3.8 syntax.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from functools import partial
from pathlib import Path
from typing import NamedTuple

TIP_MARGIN = 6
SIDECAR_NAME = re.compile(r"blk([0-9]{5})\.dat\.json")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
CHUNK_SIZE = 8 * 1024 * 1024


class ArchiveError(Exception):
    """The archive cannot serve the requested parse or update."""


class Sidecar(NamedTuple):
    number: int
    name: str
    size: int
    sha256: str
    ranges: tuple[tuple[int, int], ...]

    def lowest_height(self) -> int:
        return min(start for start, _ in self.ranges)

    def holds_any(self, low: int, high: int) -> bool:
        return any(start <= high and end >= low for start, end in self.ranges)


def as_height(value: object) -> int | None:
    """`value` when it is a non-negative JSON integer, otherwise None."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def read_sidecar(path: Path) -> Sidecar:
    match = SIDECAR_NAME.fullmatch(path.name)
    if match is None:
        raise ArchiveError(f"Bitcoin block archive has an unexpected sidecar: {path.name}")
    name = path.name[: -len(".json")]
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ArchiveError(f"Bitcoin block archive sidecar is not JSON: {path.name}: {error}") from error
    if not isinstance(entry, dict) or entry.get("schema_version") != 1 or entry.get("file") != name:
        raise ArchiveError(f"Bitcoin block archive sidecar is not schema 1 for {name}")
    size, checksum, ranges = as_height(entry.get("size")), entry.get("sha256"), entry.get("height_ranges")
    if size is None:
        raise ArchiveError(f"Bitcoin block archive sidecar has an invalid size: {name}")
    if not isinstance(checksum, str) or SHA256_HEX.fullmatch(checksum) is None:
        raise ArchiveError(f"Bitcoin block archive sidecar has an invalid checksum: {name}")
    if not isinstance(ranges, list) or not ranges:
        raise ArchiveError(f"Bitcoin block archive sidecar has no height ranges: {name}")
    pairs = []
    for pair in ranges:
        start, end = (
            (as_height(pair[0]), as_height(pair[1])) if isinstance(pair, list) and len(pair) == 2 else (None, None)
        )
        if start is None or end is None or end < start:
            raise ArchiveError(f"Bitcoin block archive sidecar has invalid height ranges: {name}")
        pairs.append((start, end))
    return Sidecar(int(match.group(1)), name, size, checksum, tuple(pairs))


def read_sidecars(directory: Path) -> list[Sidecar]:
    sidecars = [read_sidecar(path) for path in sorted(directory.glob("blk*.dat.json"))]
    if not sidecars:
        raise ArchiveError("Bitcoin block archive has no blkNNNNN.dat.json sidecars")
    return sidecars


def require_contiguous(sidecars: list[Sidecar], first: int) -> None:
    """Require the files from `first` on to be numbered without a gap."""
    expected = first
    for sidecar in sidecars:
        if sidecar.number < first:
            continue
        if sidecar.number != expected:
            raise ArchiveError(f"Bitcoin block archive has a gap before {sidecar.name}.json")
        expected += 1
    if expected == first:
        raise ArchiveError(f"Bitcoin block archive has no blk{first:05d}.dat")


def index_sha256(sidecars: list[Sidecar], last: int) -> str:
    """Digest of the files a BlockSci scan through `last` has indexed."""
    digest = hashlib.sha256()
    for sidecar in sidecars[: last + 1]:
        digest.update(f"{sidecar.name} {sidecar.sha256}\n".encode("ascii"))
    return digest.hexdigest()


def last_file_needed(sidecars: list[Sidecar], max_block: int) -> int:
    """Last file holding a height up to `max_block` plus the tip margin."""
    limit = max_block + TIP_MARGIN
    numbers = [sidecar.number for sidecar in sidecars if sidecar.lowest_height() <= limit]
    if not numbers:
        raise ArchiveError(f"Bitcoin block archive holds no height up to {limit}")
    return max(numbers)


def read_source_manifest(path: Path) -> tuple[int, int, str]:
    """The source cache's maximum block, last scanned file and index digest."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ArchiveError(f"Source cache manifest is unreadable: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("source_kind") != "bitcoin-blocks-s3":
        raise ArchiveError("Incremental update from the block archive requires a bitcoin-blocks-s3 cache")
    max_block = as_height(manifest.get("exported_max_block"))
    last_file = as_height(manifest.get("block_archive_last_file"))
    digest = manifest.get("block_archive_index_sha256")
    if last_file is None or not isinstance(digest, str) or SHA256_HEX.fullmatch(digest) is None:
        raise ArchiveError(
            "Source cache does not record which archive files it indexed; parse it again with this pipeline"
        )
    if max_block is None:
        raise ArchiveError("Source cache manifest has no exported_max_block")
    return max_block, last_file, digest


def plan_files(sidecars: list[Sidecar], max_block: int, source_manifest: Path | None) -> list[Sidecar]:
    require_contiguous(sidecars, 0)
    if source_manifest is None:
        return sidecars[: last_file_needed(sidecars, max_block) + 1]

    source_max_block, first, digest = read_source_manifest(source_manifest)
    if max_block <= source_max_block:
        raise ArchiveError(f"Target maximum block {max_block} must be greater than source {source_max_block}")
    if first >= len(sidecars) or index_sha256(sidecars, first) != digest:
        raise ArchiveError("Bitcoin block archive files indexed by the source cache have changed")
    last = max(first, last_file_needed(sidecars, max_block))
    older = [sidecar for sidecar in sidecars[:first] if sidecar.holds_any(source_max_block + 1, max_block)]
    return older + sidecars[first : last + 1]


def select(arguments: argparse.Namespace) -> None:
    """Copy the chosen sidecars, write the s5cmd download list and the manifest fields."""
    sidecars = read_sidecars(arguments.sidecars)
    chosen = plan_files(sidecars, arguments.max_block, arguments.source_manifest)
    last = chosen[-1].number
    blocks: Path = arguments.blocks
    if any(character.isspace() for character in f"{arguments.uri}{blocks}"):
        raise ArchiveError("Block archive URI and block directory must not contain whitespace")
    lines = []
    for sidecar in chosen:
        shutil.copyfile(arguments.sidecars / f"{sidecar.name}.json", blocks / f"{sidecar.name}.json")
        lines.append(f"cp {arguments.uri}/{sidecar.name} {blocks}/\n")
    arguments.commands.write_text("".join(lines), encoding="utf-8")
    arguments.manifest_fields.write_text(
        f',\n  "block_archive_last_file": {last},\n  "block_archive_index_sha256": "{index_sha256(sidecars, last)}"',
        encoding="utf-8",
    )
    total = sum(sidecar.size for sidecar in chosen)
    print(f"[block-archive] downloading {len(chosen)} of {len(sidecars)} block files ({total} bytes)")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(partial(stream.read, CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(arguments: argparse.Namespace) -> None:
    """Check downloaded files against their sidecars and the heights they must cover."""
    blocks: Path = arguments.blocks
    from_height, from_file = 0, 0
    if arguments.source_manifest is not None:
        source_max_block, from_file, _ = read_source_manifest(arguments.source_manifest)
        from_height = source_max_block + 1
    sidecars = read_sidecars(blocks)
    require_contiguous(sidecars, from_file)
    for sidecar in sidecars:
        block_path = blocks / sidecar.name
        if not block_path.is_file() or block_path.stat().st_size != sidecar.size:
            raise ArchiveError(f"Bitcoin block archive is missing or changed: {sidecar.name}")
        if file_sha256(block_path) != sidecar.sha256:
            raise ArchiveError(f"Bitcoin block archive checksum mismatch: {sidecar.name}")

    next_height = from_height
    for start, end in sorted(pair for sidecar in sidecars for pair in sidecar.ranges):
        if start > next_height:
            break
        next_height = max(next_height, end + 1)
    if next_height <= arguments.max_block:
        raise ArchiveError(
            f"Bitcoin block archive does not cover heights {from_height}..{arguments.max_block}; "
            f"height {next_height} is missing"
        )


def height(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    chooser = commands.add_parser("select", help="choose the block files to download")
    chooser.add_argument("sidecars", type=Path, help="directory holding every downloaded sidecar")
    chooser.add_argument("blocks", type=Path, help="BlockSci block directory receiving the files")
    chooser.add_argument("--uri", required=True, help="S3 prefix of the archive")
    chooser.add_argument("--max-block", type=height, required=True)
    chooser.add_argument("--source-manifest", type=Path, help="cache manifest of the index being updated")
    chooser.add_argument("--commands", type=Path, required=True, help="s5cmd run file to write")
    chooser.add_argument("--manifest-fields", type=Path, required=True, help="JSON fields for the new cache")
    chooser.set_defaults(handler=select)

    checker = commands.add_parser("verify", help="check downloaded files before parsing")
    checker.add_argument("blocks", type=Path)
    checker.add_argument("--max-block", type=height, required=True)
    checker.add_argument("--source-manifest", type=Path, help="cache manifest of the index being updated")
    checker.set_defaults(handler=verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        arguments.handler(arguments)
    except ArchiveError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
