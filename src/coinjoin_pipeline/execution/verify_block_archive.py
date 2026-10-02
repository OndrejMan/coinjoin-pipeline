"""Verify a bitcoin-block-archive copy before BlockSci parses it.

Every archived ``blkNNNNN.dat`` has a schema-1 sidecar ``blkNNNNN.dat.json``
with its size, SHA-256 and the block heights it holds. The files must start at
``blk00000.dat`` without gaps, match their sidecars, and together cover every
height from 0 through the requested maximum block.
"""

import hashlib
import json
import re
import sys
from functools import partial
from pathlib import Path

blocks_dir = Path(sys.argv[1])
requested_height = int(sys.argv[2])


def is_height(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


sidecars = sorted(blocks_dir.glob("blk*.dat.json"))
if not sidecars:
    raise SystemExit("Bitcoin block archive has no blkNNNNN.dat.json sidecars")
covered = []
for number, sidecar in enumerate(sidecars):
    name = f"blk{number:05d}.dat"
    if sidecar.name != f"{name}.json":
        raise SystemExit(f"Bitcoin block archive has a gap before {sidecar.name}")
    try:
        entry = json.loads(sidecar.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SystemExit(f"Bitcoin block archive sidecar is not JSON: {sidecar.name}: {error}") from error
    if not isinstance(entry, dict) or entry.get("schema_version") != 1 or entry.get("file") != name:
        raise SystemExit(f"Bitcoin block archive sidecar is not schema 1 for {name}")
    size, checksum, ranges = entry.get("size"), entry.get("sha256"), entry.get("height_ranges")
    if not is_height(size):
        raise SystemExit(f"Bitcoin block archive sidecar has an invalid size: {name}")
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise SystemExit(f"Bitcoin block archive sidecar has an invalid checksum: {name}")
    if not isinstance(ranges, list) or not ranges:
        raise SystemExit(f"Bitcoin block archive sidecar has no height ranges: {name}")
    for pair in ranges:
        if not isinstance(pair, list) or len(pair) != 2 or not all(map(is_height, pair)) or pair[1] < pair[0]:
            raise SystemExit(f"Bitcoin block archive sidecar has invalid height ranges: {name}")
        covered.append((pair[0], pair[1]))
    block_path = blocks_dir / name
    if not block_path.is_file() or block_path.stat().st_size != size:
        raise SystemExit(f"Bitcoin block archive is missing or changed: {name}")
    hasher = hashlib.sha256()
    with block_path.open("rb") as stream:
        for chunk in iter(partial(stream.read, 8 * 1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.hexdigest() != checksum:
        raise SystemExit(f"Bitcoin block archive checksum mismatch: {name}")

next_height = 0
for start, end in sorted(covered):
    if start > next_height:
        break
    next_height = max(next_height, end + 1)
if next_height <= requested_height:
    raise SystemExit(
        f"Bitcoin block archive does not cover heights 0..{requested_height}; height {next_height} is missing"
    )
