"""Configuration, resource guards and recovery assertions for the real sync E2E.

Both networks run the production CLI and workers. This helper never starts a
container, contacts Bitcoin peers or reads a remote service.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

import yaml

MAINNET_MAX_HEIGHT = 450_000
MAINNET_GENESIS = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def configuration(work: Path, endpoint: str, run: str, mode: str, target: int) -> dict:
    if mode not in {"mainnet", "regtest"}:
        raise ValueError("Unknown test network")
    if not 1 <= target < MAINNET_MAX_HEIGHT:
        raise ValueError("Test target must stay below the mainnet height guard")
    network = {"network": "bitcoin", "reorg_reserve": 2016}
    if mode == "regtest":
        network = {"network": "bitcoin_regtest", "scan_start_height": 1, "reorg_reserve": 288}
    return {
        "action": "mainnet-sync",
        "run_id": run,
        "runs_root": str(work / "runs"),
        "artifacts": {
            "backend": "s3",
            "uri": "s3://local-mainnet-sync/runs",
            "endpoint_url": endpoint,
            "credentials_file": str(work / "credentials"),
            "profile": "local",
        },
        "mainnet": {
            **network,
            "target_height": target,
            "root": str(work / "data"),
            "core_image": str(work / "images/core.sif"),
            "dumplings_image": str(work / "images/dumplings.sif"),
            "dbcache_mb": 1024 if mode == "mainnet" else 128,
            "poll_seconds": 1,
            "blocks_uri": "s3://local-mainnet-sync/blocks",
            "dumplings_uri": "s3://local-mainnet-sync/dumplings",
            "backup_uri": "s3://local-mainnet-sync/core",
        },
        "blocksci": {
            "index_dir": str(work / "index"),
            "index_backup_uri": "s3://local-mainnet-sync/index",
        },
        "pbs": {
            "blocksci_image": str(work / "images/blocksci.sif"),
            "producer": {
                "ncpus": 2,
                "mem": "8gb" if mode == "mainnet" else "4gb",
                "scratch": "2gb",
                "scratch_type": "local",
                "walltime": "06:00:00" if mode == "mainnet" else "00:25:00",
            },
            "blocksci": {
                "ncpus": 4,
                "mem": "16gb" if mode == "mainnet" else "8gb",
                "scratch": "4gb",
                "scratch_type": "local",
                "walltime": "03:00:00" if mode == "mainnet" else "00:20:00",
            },
        },
    }


def guard(work: Path, mode: str, max_data_mb: int) -> None:
    if mode == "mainnet":
        for name in (work / "data/runs").glob("*/producer.json"):
            # The worker replaces progress atomically; no partially written JSON is read.
            height = read_json(name).get("height")
            if height is not None and height > MAINNET_MAX_HEIGHT:
                raise ValueError(f"Mainnet height guard exceeded: {height}")
    size = 0
    for directory in ("data", "index", "minio", "recovered", "recovered-index"):
        for path in (work / directory).rglob("*"):
            try:
                if not path.is_symlink() and path.is_file():
                    size += path.stat().st_size
            except FileNotFoundError:
                pass  # Core and MinIO may rotate files while the test checks the budget.
    if size > max_data_mb * 1024 * 1024:
        raise ValueError(f"Test data guard exceeded: {size // (1024 * 1024)} MiB > {max_data_mb} MiB")


def check_height(done: dict, mode: str, target: int) -> None:
    assert done["state"] == "done"
    assert done["network"] == ("bitcoin" if mode == "mainnet" else "bitcoin_regtest")
    assert done["requested_target_height"] == target
    assert done["dumplings_height"] == done["height"]
    if mode == "mainnet":
        assert target <= done["height"] <= MAINNET_MAX_HEIGHT, "Mainnet did not stop near the bounded target"
    else:
        assert done["height"] == target


def check_first(work: Path, done_path: Path, mode: str, target: int) -> None:
    check_height(read_json(done_path), mode, target)
    if mode == "regtest":
        files = sorted((work / "data/bitcoin/regtest/blocks").glob("blk*.dat"))
        assert files and files[0].name != "blk00000.dat", "Regtest must really prune old block files"
    assert not (work / "data/.sync-active").exists()


def requires_block_archive(work: Path, mode: str) -> bool:
    chain = "bitcoin" if mode == "mainnet" else "bitcoin/regtest"
    files = sorted((work / "data" / chain / "blocks").glob("blk*.dat"))
    assert files, "Core block files are missing"
    # The archiver retains its two newest files. A bounded mainnet can fit
    # entirely in those files; regtest must still exercise archival and pruning.
    return (
        mode == "regtest"
        or (work / "data/archive-state/blk00000.dat.json").is_file()
        or files[-1].name >= "blk00002.dat"
    )


def check_final(work: Path, token: str, mode: str, target: int) -> dict:
    first = read_json(work / f"data/runs/local-sync-{token}-1/done.json")
    done = read_json(work / f"data/runs/local-sync-{token}-2/done.json")
    check_height(done, mode, target)
    assert done["height"] > first["height"], "The second generation must advance the existing index"
    chain = "bitcoin" if mode == "mainnet" else "bitcoin/regtest"
    assert (work / "recovered" / chain / "chainstate").is_dir()
    scanner = work / "recovered/dumplings/Scanner"
    assert (scanner / "Tx0Candidates.txt").is_file()
    assert int((scanner / "LastProcessedBlockHeight.txt").read_text()) == done["height"]
    if mode == "regtest":
        assert not (work / "recovered/bitcoin/regtest/blocks/blk00000.dat").exists()
    index = read_json(work / "recovered-index-info.json")
    core = read_json(work / "recovered-core-info.json")
    assert index["blocks"] == done["height"] + 1
    assert index["height"] == core["blocks"] == done["height"]
    assert index["hash"] == core["bestblockhash"] == done["hash"]
    assert core["chain"] == ("main" if mode == "mainnet" else "regtest")
    assert index["genesis"] == (work / "recovered-genesis.txt").read_text().strip()
    if mode == "mainnet":
        assert index["genesis"] == MAINNET_GENESIS, "The test must use real Bitcoin mainnet"
        # This bounded history predates Dumplings' first supported CoinJoin height.
        assert not (scanner / "Tx0Candidates.txt").read_text().strip()
    assert core["pruned"] and not core["automatic_pruning"]
    assert read_json(work / "recovered-core-indexes.json") == {}
    archive_required = requires_block_archive(work, mode)
    archive = read_json(work / "archived-oldest-block.json")
    if archive_required:
        assert archive["size"] > 0
    else:
        assert archive["state"] == "pending", "The active block files must stay in the Core checkpoint"
        assert (work / "recovered" / chain / "blocks/blk00000.dat").stat().st_size > 0
    core_backup = read_json(work / "core-latest.json")
    assert core_backup["index_backup"] == done["index_backup"]
    assert core_backup["height"] == done["height"] and core_backup["hash"] == done["hash"]
    assert not (work / "data/.sync-active").exists()
    summary = {
        "network": mode,
        "first_height": first["height"],
        "target_height": target,
        "height": done["height"],
        "hash": done["hash"],
        "genesis": index["genesis"],
        "core_checkpoint": done["core_checkpoint"],
        "index_backup": done["index_backup"],
        "block_archive_verified": archive_required,
        "recovery_verified": True,
    }
    (work / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"PASS: {mode} two-generation Core/Dumplings/archive -> PBS/BlockSci -> verified backup recovery")
    return summary


def diagnostics(work: Path, result: Path, mode: str, status: int) -> None:
    result.mkdir(parents=True, exist_ok=True)
    (result / "exit-status").write_text(str(status) + "\n")
    (result / "test-network.txt").write_text(mode + "\n")
    # Keep full evidence in work. CI gets metadata and log tails, never large data or credentials.
    roots = [(work, result), (work / "data/runs", result / "workers"), (work / "runs", result / "submission")]
    for source, destination in roots:
        files = source.iterdir() if source == work else source.rglob("*")
        for path in files:
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix not in {".json", ".yaml", ".log", ".txt", ".jobid"}
            ):
                continue
            output = destination / path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".log":
                with path.open("rb") as handle:
                    handle.seek(max(0, path.stat().st_size - 1024 * 1024))
                    output.write_bytes(handle.read())
            elif path.stat().st_size <= 4 * 1024 * 1024:
                shutil.copyfile(path, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action, names in {
        "config": ("work", "endpoint", "run", "mode", "target"),
        "guard": ("work", "mode", "max_data_mb"),
        "first": ("work", "done", "mode", "target"),
        "archive-required": ("work", "mode"),
        "final": ("work", "token", "mode", "target"),
        "diagnostics": ("work", "result", "mode", "status"),
    }.items():
        command = sub.add_parser(action)
        for name in names:
            command.add_argument(name, type=int if name in {"target", "max_data_mb", "status"} else str)
    args = parser.parse_args()
    work = Path(args.work)
    if args.action == "config":
        value = configuration(work, args.endpoint, args.run, args.mode, args.target)
        (work / f"{args.run}.yaml").write_text(yaml.safe_dump(value, sort_keys=False))
    elif args.action == "guard":
        guard(work, args.mode, args.max_data_mb)
    elif args.action == "first":
        check_first(work, Path(args.done), args.mode, args.target)
    elif args.action == "archive-required":
        print("yes" if requires_block_archive(work, args.mode) else "no")
    elif args.action == "final":
        check_final(work, args.token, args.mode, args.target)
    else:
        diagnostics(work, Path(args.result), args.mode, args.status)


if __name__ == "__main__":
    main()
