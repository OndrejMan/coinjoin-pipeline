#!/usr/bin/env python3
"""Import exported emulator block_*.json files into a regtest bitcoind."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def rpc(url: str, user: str, password: str, method: str, params: list[Any] | None = None) -> Any:
    payload = json.dumps(
        {
            "jsonrpc": "1.0",
            "id": "bitcoin-analysis-import",
            "method": method,
            "params": params or [],
        }
    ).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {token}")

    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("error") is not None:
        raise RuntimeError(data["error"])
    return data.get("result")


def wait_rpc(url: str, user: str, password: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            rpc(url, user, password, "getblockchaininfo")
            return
        except Exception as exc:  # pylint: disable=broad-exception-caught
            last_error = exc
            time.sleep(1)

    raise TimeoutError(f"bitcoind RPC did not become ready within {timeout}s: {last_error}")


def varint(value: int) -> bytes:
    if value < 0xFD:
        return value.to_bytes(1, "little")
    if value <= 0xFFFF:
        return b"\xfd" + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return b"\xfe" + value.to_bytes(4, "little")
    return b"\xff" + value.to_bytes(8, "little")


def uint32_from_hex(value: str) -> bytes:
    return int(value, 16).to_bytes(4, "little", signed=False)


def block_to_hex(block: dict[str, Any]) -> str:
    txs = block.get("tx", [])
    if not isinstance(txs, list) or not txs:
        raise ValueError(f"block {block.get('height')} has no transactions")

    raw = bytearray()
    raw.extend(int(block["version"]).to_bytes(4, "little", signed=True))
    raw.extend(bytes.fromhex(block["previousblockhash"])[::-1])
    raw.extend(bytes.fromhex(block["merkleroot"])[::-1])
    raw.extend(int(block["time"]).to_bytes(4, "little", signed=False))
    raw.extend(uint32_from_hex(block["bits"]))
    raw.extend(int(block["nonce"]).to_bytes(4, "little", signed=False))
    raw.extend(varint(len(txs)))

    for tx in txs:
        tx_hex = tx.get("hex")
        if not tx_hex:
            raise ValueError(f"transaction in block {block.get('height')} has no hex")
        raw.extend(bytes.fromhex(tx_hex))

    return raw.hex()


def block_files(block_dir: Path) -> list[Path]:
    def height(path: Path) -> int:
        match = re.fullmatch(r"block_(\d+)\.json", path.name)
        if match is None:
            return -1
        return int(match.group(1))

    return sorted(
        [path for path in block_dir.glob("block_*.json") if height(path) >= 0],
        key=height,
    )


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("block_dir", type=Path, help="Directory containing block_*.json files")
    parser.add_argument("--rpc-url", default="http://127.0.0.1:18443")
    parser.add_argument("--rpc-user", default="user")
    parser.add_argument("--rpc-pass", default="password")
    parser.add_argument("--wait", type=int, default=120)
    args = parser.parse_args()

    files = block_files(args.block_dir)
    if len(files) <= 1:
        print(f"No exported blocks to import from {args.block_dir}")
        return 0

    wait_rpc(args.rpc_url, args.rpc_user, args.rpc_pass, args.wait)
    current_height = int(rpc(args.rpc_url, args.rpc_user, args.rpc_pass, "getblockcount"))
    target_height = len(files) - 1

    if current_height >= target_height:
        print(f"bitcoind already has {current_height} blocks; target is {target_height}")
        return 0

    if current_height > 0:
        current_hash = rpc(args.rpc_url, args.rpc_user, args.rpc_pass, "getblockhash", [current_height])
        exported_hash = load_json(files[current_height]).get("hash")
        if current_hash != exported_hash:
            print(
                f"ERROR: bitcoind is on a different chain at height {current_height}: "
                f"{current_hash} != {exported_hash}",
                file=sys.stderr,
            )
            return 2

    print(f"Importing blocks {current_height + 1}..{target_height} from {args.block_dir}")
    imported = 0
    for path in files[current_height + 1 :]:
        block = load_json(path)
        block_hex = block_to_hex(block)
        try:
            result = rpc(args.rpc_url, args.rpc_user, args.rpc_pass, "submitblock", [block_hex])
        except RuntimeError as exc:
            if "duplicate" not in str(exc).lower():
                raise
            result = None

        if result is not None:
            print(f"ERROR: submitblock for {path.name} returned {result}", file=sys.stderr)
            return 2
        imported += 1

    final_height = int(rpc(args.rpc_url, args.rpc_user, args.rpc_pass, "getblockcount"))
    print(f"Imported {imported} blocks; bitcoind height is {final_height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
