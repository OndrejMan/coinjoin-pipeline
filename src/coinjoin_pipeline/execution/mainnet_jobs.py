"""The jobs of one mainnet synchronization, in their order: producer and blocksci.
Each is a function over its PBS job's ``Worker``.
"""

from __future__ import annotations

import re

from coinjoin_pipeline.execution.mainnet_worker import (
    RESUME_CHECKPOINT,
    JsonObject,
    Worker,
    atomic_json,
    is_count,
    is_sha256,
    read_json,
    resume_signature,
    scanner_complete,
)
from coinjoin_pipeline.execution.pbs.index_store import IndexStore, index_manifest


def require_ready(ready: JsonObject, run_id: str) -> int:
    if ready.get("run_id") != run_id or ready.get("state") != "ready":
        raise ValueError("Missing ready checkpoint for this exact generation")
    height = ready.get("height")
    if not is_count(height) or not is_sha256(ready.get("hash")):
        raise ValueError("Invalid ready height/hash")
    if ready.get("dumplings_height") != height:
        raise ValueError("Dumplings has not completed this generation")
    return height


def producer(worker: Worker) -> None:
    worker.stage = "producer"
    worker.acquire_dataset()
    worker.reset_state()
    atomic_json(worker.root / "current-ready.json", {"run_id": worker.run_id, "state": "preparing"})
    worker.progress("core-startup")
    worker.start_core(False)
    info = worker.synchronize_core()
    height, block_hash = info["blocks"], info["bestblockhash"]
    worker.dumps.mkdir(parents=True, exist_ok=True)
    scan_height = worker.dumps / "Scanner/LastProcessedBlockHeight.txt"
    start = int(scan_height.read_text()) + 1 if scan_height.exists() else worker.scan_start
    if info.get("pruneheight", 0) > start:
        raise ValueError("Dumplings needs blocks already pruned from this working copy; restore them first")
    scanner = worker.dumps / "Scanner"
    anchor_path = scanner / "ChainCheckpoint.json"
    if scan_height.exists():
        anchor = read_json(anchor_path)
        anchor_height = anchor.get("height")
        if not is_count(anchor_height) or not start - 1 <= anchor_height <= height:
            raise ValueError("Dumplings chain anchor does not cover its saved progress")
        if worker.rpc("getblockhash", str(anchor_height)) != anchor.get("hash"):
            raise ValueError("Dumplings checkpoint is on a different fork; explicit recovery required")
    # Bind even a partially completed scan to this frozen chain. A retry can
    # resume C# checkpoints, but never silently carry detections across a reorg.
    scanner.mkdir(parents=True, exist_ok=True)
    atomic_json(anchor_path, {"height": height, "hash": block_hash})

    def scan_progress() -> None:
        try:
            current = int(scan_height.read_text())
        except (OSError, ValueError):
            current = worker.scan_start - 1
        worker.progress(
            "dumplings-scan",
            max(0, current - worker.scan_start + 1),
            max(0, height - worker.scan_start + 1),
            height=height,
            scanner_height=current,
            target_height=height,
        )

    worker.run_command(
        worker.container_command(
            worker.c["dumplings_image"],
            "dotnet",
            "/app/Dumplings.Cli.dll",
            "sync",
            "--include-tip",
            f"--frozen-height={height}",
            f"--rpccookiefile={worker.core_dir / '.cookie'}",
            f"--rpcurl=http://127.0.0.1:{worker.port}/",
            f"--network={worker.chain}",
            *([f"--startheight={worker.scan_start}"] if worker.chain == "regtest" else []),
            cwd=worker.dumps,
        ),
        "dumplings",
        scan_progress,
    )
    scanner_complete(scanner, height)
    worker.progress("block-archive")
    worker.archive()
    if worker.rpc("getblockhash", str(height)) != block_hash or worker.rpc("getblockcount") != height:
        raise ValueError("Core changed while preparing the generation")
    worker.stop_core()
    ready = {
        "run_id": worker.run_id,
        "state": "ready",
        "height": height,
        "hash": block_hash,
        "dumplings_height": height,
        "source_dir": str(worker.core_dir),
        "network": worker.network,
        "requested_target_height": worker.c.get("target_height"),
    }
    atomic_json(worker.run / "ready.json", ready)
    atomic_json(worker.root / "current-ready.json", ready)
    worker.publish_json(worker.remote + "/ready.json", ready)
    worker.progress("ready-for-blocksci", state="done", height=height)
    worker.complete(ready)


def parse_progress(worker: Worker) -> None:
    path = worker.logs / "blocksci.log"
    if not path.exists():
        return
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 8192))
        tail = handle.read().decode("utf-8", errors="replace")
    matches = re.findall(r"(\d+(?:\.\d+)?)%([^\r\n]*)", tail)
    worker.progress(
        "blocksci-parse",
        float(matches[-1][0]) if matches else None,
        100 if matches else None,
        detail=matches[-1][1].strip() if matches else None,
    )


def blocksci(worker: Worker) -> None:
    worker.stage = "blocksci"
    worker.acquire_dataset()
    ready = read_json(worker.run / "ready.json")
    height = require_ready(ready, worker.run_id)
    if ready.get("network", worker.network) != worker.network:
        raise ValueError("The ready generation uses a different network")
    current = read_json(worker.root / "current-ready.json")
    require_ready(current, worker.run_id)
    if current != ready:
        raise ValueError("The working dataset no longer matches this ready generation")
    if ready["source_dir"] != str(worker.core_dir):
        raise ValueError("Ready generation has a different block source")
    worker.index.mkdir(parents=True, exist_ok=True)
    marker = worker.index / ".writing"
    if marker.exists():
        raise ValueError("Index is incomplete; explicitly restore the previous checkpoint before retrying")
    config = worker.index / "config.json"
    previous = None
    if config.exists():
        if not worker.index_matches_inventory():
            raise ValueError("Persistent index does not match its file inventory; restore it before updating")
        previous = read_json(worker.index / "manifest.json")
        if (
            previous.get("source_dir") != str(worker.core_dir)
            or previous.get("blocksci_image") != worker.c["blocksci_image"]
        ):
            raise ValueError("Persistent index source/image changed; explicit migration required")
        signature = resume_signature(worker.index, worker.core_dir / "blocks")
        if any(previous.get(key) != value for key, value in signature.items()):
            raise ValueError("BlockSci resume bytes do not match this Core working copy; restore the matching pair")
    elif (worker.index / "parsed").exists():
        raise ValueError("Index has data but no configuration; refusing to overwrite it")
    elif not (worker.core_dir / "blocks/blk00000.dat").is_file():
        raise ValueError("Initial BlockSci parse requires the full block history; restore it explicitly")
    marker.touch()
    worker.progress("blocksci-config")
    image = worker.c["blocksci_image"]
    if not config.exists():
        worker.run_command(
            worker.container_command(
                image,
                "blocksci_parser",
                config,
                "generate-config",
                worker.network,
                worker.index / "parsed",
                "--disk",
                worker.core_dir,
                "--max-block",
                str(height + 1),
                readonly=True,
                index_writable=True,
            ),
            "blocksci-config",
        )
    else:
        value = read_json(config)
        value["parser"]["maxBlockNum"] = height + 1
        atomic_json(config, value)
    (worker.index / RESUME_CHECKPOINT).unlink(missing_ok=True)
    worker.run_command(
        worker.container_command(image, "blocksci_parser", config, "update", readonly=True, index_writable=True),
        "blocksci",
        lambda: parse_progress(worker),
    )
    signature = resume_signature(worker.index, worker.core_dir / "blocks")
    output = worker.work / "verified.json"
    worker.progress("blocksci-verify")
    verify = [
        "/usr/bin/python3",
        str(worker.work / "verify_chain.py"),
        str(config),
        "--max-block",
        str(height),
        "--network",
        worker.network,
        "--expected-block-hash",
        ready["hash"],
        "--output",
        str(output),
    ]
    if previous is not None and previous.get("exported_max_block", height) < height:
        atomic_json(worker.work / "previous.json", previous)
        verify += ["--source-manifest", str(worker.work / "previous.json")]
    worker.run_command(
        worker.container_command(
            image,
            *verify,
            readonly=True,
            environment={"PYTHONPATH": "/blocksci/.venv/lib/python3.8/site-packages:/mnt/blocksci/blockscipy"},
        ),
        "blocksci-verify",
    )
    verified = read_json(output)
    manifest = index_manifest(
        worker.run_id,
        image,
        "external-bitcoin",
        worker.network,
        verified["exported_max_block"],
        {**verified, "source_dir": str(worker.core_dir), "pbs_resources": worker.c["pbs_resources"], **signature},
    )
    # The index is complete; its backup is the checkpoint job's, so a failed upload
    # leaves a usable working copy.
    manifest = IndexStore(str(worker.index), None).publish(config, manifest)
    worker.publish_json(worker.remote + "/blocksci-index-manifest.json", manifest)
    worker.progress("indexed", state="done", height=height)
    worker.complete(manifest)
