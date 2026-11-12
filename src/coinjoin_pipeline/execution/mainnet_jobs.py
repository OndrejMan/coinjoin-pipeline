"""The jobs of one mainnet synchronization, in their order: restore, producer, blocksci
and checkpoint. Each is a function over its PBS job's ``Worker``.

Working data are downloaded only by the restore job and only into an empty working
dataset. Each backup has one place: the old archive is deleted, the new one streamed and
its size checked, and manifest.json, written last, makes it valid again.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import time
from collections.abc import Callable
from pathlib import Path

from coinjoin_pipeline.execution.mainnet_worker import (
    RESUME_CHECKPOINT,
    SIGNATURE_KEYS,
    JsonObject,
    Worker,
    atomic_json,
    file_sizes,
    is_count,
    is_sha256,
    megabytes_per_second,
    read_json,
    resume_file,
    resume_signature,
    scanner_complete,
    stopped_core,
)
from coinjoin_pipeline.execution.pbs.index_store import ARCHIVE as INDEX_ARCHIVE
from coinjoin_pipeline.execution.pbs.index_store import MEMBERS as INDEX_MEMBERS
from coinjoin_pipeline.execution.pbs.index_store import IndexStore, index_manifest, utc_now


def require_ready(ready: JsonObject, run_id: str) -> int:
    if ready.get("run_id") != run_id or ready.get("state") != "ready":
        raise ValueError("Missing ready checkpoint for this exact generation")
    height = ready.get("height")
    if not is_count(height) or not is_sha256(ready.get("hash")):
        raise ValueError("Invalid ready height/hash")
    if ready.get("dumplings_height") != height:
        raise ValueError("Dumplings has not completed this generation")
    return height


def finish_restore(worker: Worker, action: str, **extra: object) -> None:
    worker.progress("complete", None, None, "done", action=action, **extra)
    worker.complete({"run_id": worker.run_id, "action": action, **extra})


def measured(name: str, action: Callable[[], object], downloaded: int, target: Path) -> JsonObject:
    """Run one restore step; download, unpacking and checks overlap, so it has one duration."""
    started, cpu = time.monotonic(), sum(os.times()[:4])
    action()
    seconds = time.monotonic() - started
    sizes = file_sizes(target)
    unpacked = sum(sizes.values())
    part = {
        "seconds": round(seconds, 1),
        "cpu_seconds": round(sum(os.times()[:4]) - cpu, 1),
        "downloaded_bytes": downloaded,
        "unpacked_bytes": unpacked,
        "files": len(sizes),
        "downloaded_mb_per_s": megabytes_per_second(downloaded, seconds),
        "unpacked_mb_per_s": megabytes_per_second(unpacked, seconds),
    }
    print(f"restore part {name}: {json.dumps(part)}", flush=True)
    return part


def restore(worker: Worker) -> None:
    """Job 0: fill an empty working dataset from the S3 backups, otherwise do nothing.

    The working copy on multijob storage can expire. Only a completely empty one is
    restored, from three backups of one synchronization; a partial one is never mixed
    with backups, and the producer refuses it. A dataset whose index a failed job left
    unfinished is emptied first, but only once the backups replacing it check out.
    """
    worker.stage = "restore"
    worker.acquire_dataset()
    worker.reset_state()
    # Only a killed restore leaves these; both locks are held, so none is in use.
    leftovers = (".restore-*", ".clear-*")
    for leftover in (path for base in (worker.root, worker.index) for name in leftovers for path in base.glob(name)):
        shutil.rmtree(leftover)
    present = worker.dataset_members()
    unfinished = (worker.index / ".writing").exists()
    if present and not unfinished:
        finish_restore(worker, "skipped-not-empty", present=[str(path) for path in present])
        return
    backups = {
        name: prefix.rstrip("/") + "/manifest.json"
        for name, prefix in (
            ("core", worker.c["backup_uri"]),
            ("index", worker.c["index_backup_uri"]),
            ("dumplings", worker.c["dumplings_uri"]),
        )
    }
    worker.progress("restore-check")
    manifests = {
        name: worker.storage.cat_json(uri, timeout=120) if worker.storage.exists(uri) else None
        for name, uri in backups.items()
    }
    found = {name: value for name, value in manifests.items() if value is not None}
    missing = sorted(backups.keys() - found.keys())
    if not found:
        if unfinished:
            raise ValueError("The index is unfinished and S3 holds no backups to replace it")
        if worker.storage.listing(worker.c["blocks_uri"].rstrip("/") + "/*"):
            raise ValueError(
                "S3 holds archived blocks but no backups; a new initial download would mix its block files "
                "with the archived ones. Restore the backups or empty " + worker.c["blocks_uri"]
            )
        finish_restore(worker, "skipped-no-backup")
        return
    if missing:
        raise ValueError("S3 lacks the backups " + ", ".join(missing) + "; the working dataset cannot be restored")
    core, index, dumplings = found["core"], found["index"], found["dumplings"]
    mismatches = [
        label
        for label, matches in (
            ("the Core checkpoint names another index backup", core.get("index_backup") == backups["index"]),
            ("run IDs differ", core.get("run_id") == index.get("run_id") == dumplings.get("run_id")),
            (
                "heights differ",
                core.get("height") == index.get("exported_max_block") == dumplings.get("last_processed_height"),
            ),
            ("parser resume bytes differ", all(core.get(key) == index.get(key) for key in SIGNATURE_KEYS)),
        )
        if not matches
    ]
    if mismatches:
        raise ValueError("The S3 backups do not belong together: " + "; ".join(mismatches))
    if unfinished:
        worker.discard_dataset()
    started = time.monotonic()
    worker.progress("restore-download", height=core["height"])
    staging = worker.root / f".restore-{worker.run_id}"
    index_staging = worker.index / f".restore-{worker.run_id}"
    try:
        staging.mkdir()
        index_staging.mkdir(parents=True)
        parts = {
            "core": measured(
                "core",
                lambda: worker.storage.restore_backup(backups["core"], staging / "core"),
                core["archive_size"],
                staging / "core",
            ),
            "dumplings": measured(
                "dumplings",
                lambda: worker.restore_dumplings(backups["dumplings"], staging / "Scanner"),
                sum(dumplings["files"].values()),
                staging / "Scanner",
            ),
            "index": measured(
                "index",
                lambda: worker.storage.restore_backup(backups["index"], index_staging / "index"),
                index["archive_size"],
                index_staging / "index",
            ),
        }
        # Every backup is downloaded and verified; only now does any of it take its place.
        for entry in (staging / "core").iterdir():
            entry.rename(worker.root / entry.name)
        worker.dumps.mkdir()
        (staging / "Scanner").rename(worker.dumps / "Scanner")
        for entry in (index_staging / "index").iterdir():
            entry.rename(worker.index / entry.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(index_staging, ignore_errors=True)
    if not worker.index_matches_inventory():
        raise ValueError("The restored index does not match its file inventory")
    if resume_signature(worker.index, worker.core_dir / "blocks") != {key: core[key] for key in SIGNATURE_KEYS}:
        raise ValueError("The restored index and Core blocks do not belong together")
    finish_restore(
        worker,
        "restored",
        height=core["height"],
        hash=core["hash"],
        backups=backups,
        parts=parts,
        total_seconds=round(time.monotonic() - started, 1),
        host=socket.gethostname(),
    )


def require_complete_dataset(worker: Worker) -> None:
    """Refuse a partly lost working dataset before Core starts.

    The restore job fills only an empty dataset. An unfinished first synchronization
    (unpruned blocks, no index yet) continues; anything else that is missing is an error.
    """
    present = {
        "Core chainstate": (worker.core_dir / "chainstate").is_dir(),
        "Core blocks": (worker.core_dir / "blocks").is_dir(),
        "block archive state": worker.archive_state.is_dir(),
        "Dumplings Scanner": (worker.dumps / "Scanner/LastProcessedBlockHeight.txt").is_file(),
        "BlockSci index": (worker.index / "config.json").is_file(),
    }
    if not any(present.values()):
        try:
            restored = read_json(worker.run / "restore.json")
        except (OSError, ValueError):
            restored = {}
        if restored.get("run_id") != worker.run_id or restored.get("action") != "skipped-no-backup":
            raise ValueError("The working dataset is empty but this synchronization's restore job did not check S3")
        return
    missing = [name for name, found in present.items() if not found]
    unfinished_first = not present["BlockSci index"] and (worker.core_dir / "blocks/blk00000.dat").is_file()
    if missing and not unfinished_first:
        raise ValueError(
            "The working dataset is incomplete (missing: " + ", ".join(missing) + "). Remove "
            f"{worker.root} and {worker.index} entirely; the next synchronization restores both from S3."
        )


def producer(worker: Worker) -> None:
    worker.stage = "producer"
    worker.acquire_dataset()
    require_complete_dataset(worker)
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


def checkpoint(worker: Worker) -> None:
    """Job 3: back up the index and Dumplings, prune Core and back up its checkpoint, with little memory.

    Holds the index reader lock: notebooks keep reading, index jobs cannot write.
    """
    worker.stage = "checkpoint"
    worker.acquire_dataset()
    ready = read_json(worker.run / "ready.json")
    height = require_ready(ready, worker.run_id)
    if read_json(worker.root / "current-ready.json") != ready:
        raise ValueError("The working dataset no longer matches this ready generation")
    manifest = read_json(worker.index / "manifest.json")
    if (
        (worker.index / ".writing").exists()
        or manifest.get("run_id") != worker.run_id
        or manifest.get("exported_max_block") != height
        or not worker.index_matches_inventory()
    ):
        raise ValueError("The index is not the one this synchronization's BlockSci job completed")
    number = resume_file(worker.index, worker.core_dir / "blocks")
    signature = resume_signature(worker.index, worker.core_dir / "blocks")
    if any(manifest.get(key) != value for key, value in signature.items()):
        raise ValueError("BlockSci resume bytes no longer match this Core working copy")
    worker.progress("index-backup")
    index_backup = worker.backup(worker.index, INDEX_MEMBERS, worker.c["index_backup_uri"], manifest, INDEX_ARCHIVE)
    # Here, not in the producer: a run that fails earlier must leave the S3 backups matching.
    dumplings_backup = worker.publish_dumplings(ready["dumplings_height"])
    worker.progress("pruning")
    # Offline Core only loads its chain for pruning; a large cache would just cost memory.
    info = worker.start_core(True, dbcache_mb=min(1024, worker.c["dbcache_mb"]))
    if info["blocks"] != height or info["bestblockhash"] != ready["hash"]:
        raise ValueError("Offline Core does not match the ready generation")
    worker.archive(prune=True, height=min(height, ready["dumplings_height"]), number=number)
    worker.stop_core()
    if resume_signature(worker.index, worker.core_dir / "blocks") != signature:
        raise ValueError("Core cleanup changed the BlockSci resume source; checkpoint refused")
    worker.progress("core-checkpoint")
    with stopped_core(worker.core_dir):
        core_checkpoint = worker.backup(
            worker.root,
            [
                str((worker.core_dir / "blocks").relative_to(worker.root)),
                str((worker.core_dir / "chainstate").relative_to(worker.root)),
                # Scanner/ is restored from its own incremental copy (publish_dumplings).
                "archive-state",
            ],
            worker.c["backup_uri"],
            {"height": height, "hash": ready["hash"], "index_backup": index_backup, **signature},
            "checkpoint.tar.zst",
        )
    done = {
        **ready,
        "state": "done",
        "core_checkpoint": core_checkpoint,
        "index_backup": index_backup,
        "dumplings_backup": dumplings_backup,
        # Measured while Core is stopped and no index job can write.
        "storage": {
            "measured_at": utc_now(),
            "multijob": {
                "core": worker.uploaded.get(core_checkpoint, {}).get("member_bytes"),
                "index": worker.uploaded.get(index_backup, {}).get("member_bytes"),
            },
            "s3": {
                "core_checkpoint": worker.uploaded.get(core_checkpoint, {}).get("archive_size"),
                "index_backup": worker.uploaded.get(index_backup, {}).get("archive_size"),
            },
        },
    }
    worker.progress("complete", state="done", height=height)
    worker.publish_json(worker.remote + "/done.json", done)
    worker.complete(done)
    atomic_json(worker.run / "done.json", done)
