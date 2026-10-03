"""Verify the parsed index before publishing an S3 block-archive cache."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def is_block_hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def verify_chain(chain, maximum, network, expected_hash=None, source_manifest=None):
    if maximum < 0:
        raise ValueError("The requested block height must not be negative")
    if network == "bitcoin" and expected_hash is None:
        raise ValueError("Mainnet block-archive verification requires an expected block hash")
    if expected_hash is not None and not is_block_hash(expected_hash):
        raise ValueError("Expected block hash must be 64 lowercase hexadecimal characters")
    actual_count = len(chain)
    if actual_count != maximum + 1:
        raise ValueError(f"Parsed chain has {actual_count} blocks; expected {maximum + 1} through height {maximum}")
    tip = chain[maximum]
    actual_hash = str(tip.hash)
    if tip.height != maximum or not is_block_hash(actual_hash):
        raise ValueError("Parsed chain has an invalid tip height or hash")
    if expected_hash is not None and actual_hash != expected_hash:
        raise ValueError(f"Parsed block {maximum} hash {actual_hash} does not match checkpoint {expected_hash}")
    if source_manifest is not None:
        source_height = source_manifest.get("exported_max_block")
        source_hash = source_manifest.get("exported_block_hash")
        if (
            not isinstance(source_height, int)
            or isinstance(source_height, bool)
            or not 0 <= source_height < maximum
            or not is_block_hash(source_hash)
        ):
            raise ValueError("Source cache lacks a verified height/hash; parse it again with this pipeline")
        if str(chain[source_height].hash) != source_hash:
            raise ValueError(f"Updated chain does not preserve source cache block {source_height}")
    return {"exported_max_block": tip.height, "exported_block_hash": actual_hash}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--max-block", type=int, required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument("--expected-block-hash")
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    # Never let a previous successful verification survive a failed retry.
    args.output.unlink(missing_ok=True)
    source = None
    if args.source_manifest is not None:
        source = json.loads(args.source_manifest.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise ValueError("Source cache manifest must be a JSON object")
    import blocksci  # pylint: disable=import-error

    # Zero exposes every parsed block; a positive limit would hide overlong indexes.
    chain = blocksci.Blockchain(str(args.config), 0)
    result = verify_chain(chain, args.max_block, args.network, args.expected_block_hash, source)
    args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
