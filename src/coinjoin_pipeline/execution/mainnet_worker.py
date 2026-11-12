"""What every job of a mainnet synchronization shares: its settings, the working dataset and
its lock, containers, Bitcoin Core, Dumplings and the single backups in S3.

The jobs themselves are ``mainnet_jobs``; ``mainnet_runtime`` runs one of them. Standard
library only and Python 3.8 syntax, like every module a PBS job ships.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:  # TypeGuard needs Python 3.10; the PBS node never evaluates annotations.
    from typing import TypeGuard

from exporters.artifact_paths import pbs_marker

from coinjoin_pipeline.execution.pbs.apptainer import MISSING_RUNTIME, alias_binds, exec_command, find_runtime
from coinjoin_pipeline.execution.pbs.index_store import inventory as index_inventory
from coinjoin_pipeline.execution.pbs.index_store import usage_report, utc_now
from coinjoin_pipeline.storage.s5client import SHA256, S5Client, S5Error

# Core spans methods; cleanup below owns its lifetime rather than a single local with block.
# pylint: disable=consider-using-with


# Free-form JSON documents: manifests, progress records, Core RPC answers.
JsonObject = dict


class PbsResources(TypedDict):
    ncpus: int
    mem: str


class Settings(TypedDict, total=False):
    """settings.json, written by mainnet_sync.render_sync for one job."""

    run_id: str
    root: str
    index_dir: str
    log_root: str
    network: str
    scan_start_height: int | None
    target_height: int | None
    state_uri: str
    backup_uri: str
    blocks_uri: str
    dumplings_uri: str
    index_backup_uri: str
    endpoint: str
    credentials: str
    profile: str
    core_image: str
    dumplings_image: str
    blocksci_image: str
    dbcache_mb: int
    reorg_reserve: int
    poll_seconds: float
    pbs_resources: PbsResources
    sync_stages: list[str]
    notebook_root: str | None
    # restore and blocksci write the index, the checkpoint reads it; producer leaves it alone.
    index_lock: str | None
    index_lock_seconds: int


CHUNK = 8 * 1024 * 1024


# The parser's resume point; its bytes tie an index to one Core block directory.
RESUME_CHECKPOINT = "parsed/parser/blockList.dat.resume.json"


def read_json(path: Path) -> JsonObject:
    return json.loads(path.read_text())


def is_count(value: object) -> TypeGuard[int]:
    """A plain non-negative integer; JSON booleans do not count."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def file_sizes(root: Path) -> dict[str, int]:
    """Relative path -> size of every regular file below root."""
    return {str(path.relative_to(root)): path.stat().st_size for path in sorted(root.rglob("*")) if path.is_file()}


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def scanner_complete(scanner: Path, height: int) -> None:
    if int((scanner / "LastProcessedBlockHeight.txt").read_text()) != height:
        raise ValueError("Dumplings did not reach the frozen Core height")
    # Missing evidence is a failure too, not an empty misses list.
    for name in ("Checkpoint.txt", "Tx0Candidates.txt", "Tx0CandidateMisses.txt", "ChainCheckpoint.json"):
        if not (scanner / name).is_file():
            raise ValueError(f"Dumplings recovery state is missing {name}")
    if (scanner / "Tx0CandidateMisses.txt").read_text().strip():
        raise ValueError("Dumplings has unresolved Tx0 candidates; publication/pruning refused")
    lines = (scanner / "Checkpoint.txt").read_text().splitlines()
    if not lines or int(lines[0]) != height:
        raise ValueError("Dumplings checkpoint and published height disagree")
    lengths = {}
    for line in lines[1:]:
        name, size = line.rsplit(" ", 1)
        if Path(name).name != name or int(size) < 0 or name in lengths:
            raise ValueError("Invalid Dumplings checkpoint file list")
        lengths[name] = int(size)
        path = scanner / name
        if (path.stat().st_size if path.is_file() else 0) != int(size):
            raise ValueError("Dumplings checkpoint file length mismatch: " + name)
    if not {"Tx0Candidates.txt", "Tx0CandidateMisses.txt"} <= lengths.keys():
        raise ValueError("Dumplings checkpoint has no Tx0 recovery state")
    anchor = read_json(scanner / "ChainCheckpoint.json")
    if anchor.get("height") != height or not is_sha256(anchor.get("hash")):
        raise ValueError("Dumplings checkpoint has no matching chain anchor")


@contextmanager
def stopped_core(datadir: Path) -> Iterator[None]:
    # A running Core holds a POSIX write lock on .lock: the shared lock fails while one
    # runs, and while it is held no Core can start and open the datadir mid-backup.
    with (datadir / ".lock").open("rb") as handle:
        fcntl.lockf(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if (datadir / ".cookie").exists():
            raise ValueError("Core still has an RPC cookie; checkpoint refused")
        yield


def resume_file(index: Path, blocks: Path) -> int:
    checkpoint = read_json(index / RESUME_CHECKPOINT)
    number = checkpoint.get("file_number")
    if checkpoint.get("schema_version") != 1 or not is_count(number):
        raise ValueError("Invalid BlockSci parser resume checkpoint")
    source = blocks / f"blk{number:05d}.dat"
    offset, size = checkpoint.get("data_offset"), checkpoint.get("block_size")
    if not is_count(offset) or not is_count(size) or offset < 8 or size < 80:
        raise ValueError("Invalid BlockSci parser resume offset")
    if not source.is_file() or source.stat().st_size < offset + size:
        raise ValueError("BlockSci parser resume file is missing or truncated")
    return number


def resume_signature(index: Path, blocks: Path) -> dict[str, object]:
    number = resume_file(index, blocks)
    checkpoint = read_json(index / RESUME_CHECKPOINT)
    end = checkpoint["data_offset"] + checkpoint["block_size"]
    remaining, digest = end, hashlib.sha256()
    with (blocks / f"blk{number:05d}.dat").open("rb") as handle:
        while remaining:
            chunk = handle.read(min(remaining, CHUNK))
            if not chunk:
                raise ValueError("Parser resume source was truncated while hashing")
            remaining -= len(chunk)
            digest.update(chunk)
    return {
        "resume_file_number": number,
        "resume_file_prefix_size": end,
        "resume_file_prefix_sha256": digest.hexdigest(),
    }


class Worker:
    def __init__(self, settings: Settings, work: Path) -> None:
        self.c = settings
        self.work = work
        self.root = Path(settings["root"])
        self.network = settings.get("network", "bitcoin")
        self.chain = "regtest" if self.network == "bitcoin_regtest" else "main"
        self.datadir = self.root / "bitcoin"
        self.core_dir = self.datadir / "regtest" if self.chain == "regtest" else self.datadir
        self.scan_start = settings.get("scan_start_height") or (1 if self.chain == "regtest" else 336861)
        self.dumps = self.root / "dumplings"
        self.archive_state = self.root / "archive-state"
        self.index = Path(settings["index_dir"])
        self.run_id = settings["run_id"]
        # One synchronization's state: each run replaces the previous one, only its logs stay.
        self.run = self.root / "sync"
        self.active = self.root / ".sync-active"
        self.remote = settings["state_uri"].rstrip("/")
        self.storage = S5Client(settings["endpoint"], settings["credentials"], settings["profile"])
        self.core: subprocess.Popen | None = None
        self.children: list[subprocess.Popen] = []
        # Manifest URI -> sizes of the backups this job uploaded, for done.json.
        self.uploaded: dict[str, JsonObject] = {}
        self.port: int | None = None
        self.stage = ""
        self.container = find_runtime()

    def publish_json(self, uri: str, value: object) -> None:
        path = self.work / "publish.json"
        atomic_json(path, value)
        self.storage.copy(path, uri)

    def progress(
        self,
        phase: str,
        current: float | None = None,
        total: float | None = None,
        state: str = "running",
        **extra: object,
    ) -> None:
        percent = None if total is None or current is None or total <= 0 else round(min(100, 100 * current / total), 2)
        value = {
            "run_id": self.run_id,
            "stage": self.stage,
            "phase": phase,
            "state": state,
            "current": current,
            "total": total,
            "percent": percent,
            "updated_at": utc_now(),
            **extra,
        }
        atomic_json(self.run / f"{self.stage}.json", value)
        self.publish_json(f"{self.remote}/{self.stage}.json", value)
        print(json.dumps(value), flush=True)

    def owns_dataset(self) -> bool:
        try:
            return read_json(self.active / "owner.json").get("run_id") == self.run_id
        except (OSError, ValueError):
            return False

    def acquire_dataset(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # mkdir is atomic on the shared filesystem. No expiry guesses across queue waits.
        self.active.mkdir()
        atomic_json(self.active / "owner.json", {"run_id": self.run_id, "job_id": os.environ.get("PBS_JOBID")})

    def release_dataset(self) -> None:
        if self.owns_dataset():
            shutil.rmtree(self.active)

    def complete(self, value: JsonObject) -> None:
        """Publish this job's .done marker, which releases its PBS dependents, and free the dataset."""
        self.publish_json(f"{self.remote}/{pbs_marker('mainnet-' + self.stage, 'done')}", value)
        self.release_dataset()

    def index_matches_inventory(self) -> bool:
        return (self.index / "files.txt").read_text() == index_inventory(self.index)

    def reset_state(self) -> None:
        """Clear the previous synchronization's state; its logs stay under logs/<run-id>/."""
        stages = self.c["sync_stages"]
        # <stage>.json is each job's progress.
        names = ("ready.json", "done.json", "blocksci-index-manifest.json", *(f"{stage}.json" for stage in stages))
        for name in (*names, *(f"{stage}.quiescent" for stage in stages if stage != self.stage)):
            (self.run / name).unlink(missing_ok=True)
        for name in names:
            self.storage.remove(f"{self.remote}/{name}")
        for stage in stages:
            for outcome in ("done", "failed"):
                self.storage.remove(f"{self.remote}/{pbs_marker('mainnet-' + stage, outcome)}")

    def container_command(
        self,
        image: str,
        *args: object,
        cwd: Path | None = None,
        readonly: bool = False,
        index_writable: bool = False,
        environment: dict[str, str] | None = None,
        binds: Sequence[str] = (),
    ) -> list[str]:
        """The dataset, the job directory and, for BlockSci, the index are bound at their own paths."""
        if self.container is None:
            raise RuntimeError(MISSING_RUNTIME)
        mounts = [*alias_binds(self.root, "ro" if readonly else "rw"), *alias_binds(self.work)]
        if self.index.exists() and image == self.c["blocksci_image"]:
            mounts += alias_binds(self.index, "rw" if index_writable else "ro")
        return exec_command(
            self.container,
            image,
            args,
            binds=[*mounts, *binds],
            environment=[f"{key}={value}" for key, value in (environment or {}).items()],
            cleanenv=True,
            pwd=cwd,
        )

    def run_command(self, command: list[str], name: str, poll: Callable[[], None] | None = None) -> subprocess.Popen:
        self.logs.mkdir(parents=True, exist_ok=True)
        with (self.logs / f"{name}.log").open("ab") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
            )
            self.children.append(process)
            while process.poll() is None:
                if poll:
                    poll()
                time.sleep(self.c["poll_seconds"])
            if process.returncode:
                raise RuntimeError(f"{name} exited with {process.returncode}; see {self.logs / (name + '.log')}")
        return process

    @property
    def logs(self) -> Path:
        """Small component logs live on the filesystem shared with the frontend."""
        return Path(self.c["log_root"]) / f"mainnet-{self.stage}"

    def rpc(self, method: str, *args: str) -> object:
        command = self.container_command(
            self.c["core_image"],
            "bitcoin-cli",
            f"-datadir={self.datadir}",
            f"-chain={self.chain}",
            f"-conf={self.work / 'bitcoin.conf'}",
            f"-rpcport={self.port}",
            "-rpcclienttimeout=15",
            method,
            *args,
        )
        output = subprocess.run(
            command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25
        ).stdout
        try:
            return json.loads(output)
        except ValueError:
            return output.strip()

    def rpc_info(self, method: str) -> JsonObject:
        """An RPC that answers with an object, such as getblockchaininfo."""
        value = self.rpc(method)
        if not isinstance(value, dict):
            raise ValueError(f"Core {method} did not return an object")
        return value

    def start_core(self, offline: bool, dbcache_mb: int | None = None) -> JsonObject:
        self.core_dir.mkdir(parents=True, exist_ok=True)
        xor = self.core_dir / "blocks/xor.dat"
        if xor.exists() and any(xor.read_bytes()):
            raise ValueError("Existing XOR block files are incompatible with BlockSci; migrate them explicitly")
        if any(path.name != "chainstate" for path in self.core_dir.glob("chainstate*")):
            raise ValueError("Multiple chainstates require an explicit migration before checkpointing")
        (self.work / "bitcoin.conf").write_text("")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        command = self.container_command(
            self.c["core_image"],
            "bitcoind",
            f"-datadir={self.datadir}",
            f"-chain={self.chain}",
            *(["-fastprune=1"] if self.chain == "regtest" else []),
            f"-conf={self.work / 'bitcoin.conf'}",
            "-server=1",
            "-daemon=0",
            "-listen=0",
            "-prune=1",
            "-txindex=0",
            "-blocksxor=0",
            "-disablewallet=1",
            "-rpcbind=127.0.0.1",
            "-rpcallowip=127.0.0.1",
            f"-rpcport={self.port}",
            f"-dbcache={dbcache_mb or self.c['dbcache_mb']}",
            f"-networkactive={0 if offline or self.chain == 'regtest' else 1}",
        )
        self.logs.mkdir(parents=True, exist_ok=True)
        with (self.logs / f"core-{self.stage}.log").open("ab") as log:
            self.core = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        while self.core.poll() is None:
            try:
                info = self.rpc_info("getblockchaininfo")
                if info.get("chain") != self.chain or info.get("automatic_pruning") is not False:
                    raise ValueError("Core must use the configured network and manual pruning")
                if self.rpc("getindexinfo"):
                    raise ValueError("The synchronization dataset must have txindex and optional indexes disabled")
                return info
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                self.progress("core-startup")
                time.sleep(self.c["poll_seconds"])
        raise RuntimeError("Core failed to start; see its persistent log")

    def stop_core(self, *, require_clean_exit: bool = True) -> None:
        if self.core is None:
            return
        if self.core.poll() is None:
            self.rpc("stop")
        # A checkpoint is forbidden until bitcoind has fully flushed and exited.
        code = self.core.wait(timeout=600)
        self.core = None
        if require_clean_exit and code != 0:
            raise RuntimeError("Core did not shut down cleanly")

    def archive(self, prune: bool = False, height: int = 0, number: int = 0) -> None:
        # Bitcoin Core 29.1 refuses pruning before these network thresholds.
        # The regtest worker starts Core with -fastprune.
        if prune and height < (100 if self.chain == "regtest" else 100000):
            print("Historical test chain is too short for Core pruning; retaining all blocks", flush=True)
            return
        wrapper = self.work / "archive-bitcoin-cli"
        wrapper.write_text(
            "#!/bin/sh\nexec bitcoin-cli -chain="
            + self.chain
            + " -conf="
            + shlex.quote(str(self.work / "bitcoin.conf"))
            + ' "$@"\n'
        )
        wrapper.chmod(0o700)
        command = self.container_command(
            self.c["core_image"],
            "bitcoin-block-archive",
            "--bitcoin-cli",
            wrapper,
            "--block-dir",
            self.core_dir / "blocks",
            "--state-dir",
            self.archive_state,
            "--bitcoin-datadir",
            self.datadir,
            "--rpc-port",
            self.port,
            "--destination",
            self.c["blocks_uri"],
            "--endpoint",
            self.c["endpoint"],
            "--profile",
            self.c["profile"],
            "--credentials",
            self.c["credentials"],
            "--rpc-timeout",
            "120",
            # Credentials are bound only to the archiver, never copied into the dataset.
            binds=alias_binds(self.c["credentials"], "ro"),
        )
        if prune:
            ceiling = self.work / "prune-ceiling.txt"
            ceiling.write_text(str(max(0, height - self.c["reorg_reserve"])))
            command += ["--prune-only", "--prune-ceiling-file", str(ceiling), "--prune-retain-from-file", str(number)]
        self.run_command(command, "pruning" if prune else "archiver")

    def backup(self, root: Path, members: Sequence[str], prefix: str, metadata: JsonObject, name: str) -> str:
        """Replace the single backup under prefix (s5client.replace_backup); its manifest URI."""
        manifest = self.storage.replace_backup(
            root, members, prefix, name, {**metadata, "run_id": self.run_id}, self.c["pbs_resources"]["ncpus"]
        )
        manifest_uri = prefix.rstrip("/") + "/manifest.json"
        self.uploaded[manifest_uri] = {key: manifest[key] for key in ("archive_size", "member_bytes")}
        return manifest_uri

    def publish_dumplings(self, height: int) -> str:
        """Mirror Scanner/ incrementally; manifest.json, written last, makes the copy readable.

        Only changed files travel. The copy is checked by object sizes, never read back.
        """
        scanner = self.dumps / "Scanner"
        scanner_complete(scanner, height)
        prefix = self.c["dumplings_uri"].rstrip("/")
        manifest_uri = prefix + "/manifest.json"
        files = file_sizes(scanner)
        self.storage.remove(manifest_uri)
        self.progress("dumplings-backup")
        self.storage.run("sync", "--delete", str(scanner) + "/", prefix + "/Scanner/", check=True, timeout=6 * 3600)
        if self.storage.sizes(prefix + "/Scanner/") != files:
            raise ValueError("The Dumplings copy in S3 does not match Scanner/")
        manifest = {
            "run_id": self.run_id,
            "last_processed_height": height,
            "files": files,
            "upload_validation": "size-only",
            "uploaded_at": utc_now(),
        }
        self.publish_json(manifest_uri, manifest)
        return manifest_uri

    def synchronize_core(self) -> JsonObject:
        target = self.c.get("target_height")
        last_report = None
        while True:
            try:
                info = self.rpc_info("getblockchaininfo")
                if target is not None:
                    if info["blocks"] >= target:
                        break
                    now = time.monotonic()
                    if last_report is None or now - last_report >= self.c["poll_seconds"]:
                        self.progress(
                            "core-sync",
                            info["blocks"],
                            target,
                            height=info["blocks"],
                            headers=info.get("headers"),
                            target_height=target,
                        )
                        last_report = now
                else:
                    self.progress(
                        "core-sync",
                        info.get("verificationprogress", 0),
                        1,
                        height=info.get("blocks"),
                        headers=info.get("headers"),
                        estimated=True,
                    )
                    if (
                        not info.get("initialblockdownload", True)
                        and info["blocks"] == info["headers"]
                        and (self.chain == "regtest" or self.rpc_info("getnetworkinfo")["connections"] > 0)
                    ):
                        break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                self.progress("core-rpc-wait")
            if self.core is None or self.core.poll() is not None:
                raise RuntimeError("Core stopped during synchronization")
            # Bounded tests must not keep downloading for a full reporting interval.
            time.sleep(min(self.c["poll_seconds"], 1) if target is not None else self.c["poll_seconds"])
        self.rpc("setnetworkactive", "false")
        # Disabling P2P does not drain blocks already being validated. A clean
        # offline restart makes the height stable before Dumplings pins it.
        self.stop_core()
        self.start_core(True)
        frozen = self.rpc_info("getblockchaininfo")
        if target is not None:
            if frozen["blocks"] < target:
                raise ValueError("Core fell below the requested height while freezing the chain")
            self.progress(
                "core-sync",
                frozen["blocks"],
                target,
                height=frozen["blocks"],
                target_height=target,
            )
        return frozen

    def cleanup(self) -> None:
        for process in self.children:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=30)
        if self.core is not None:
            # Cleanup needs proof of process termination; checkpoint callers
            # additionally require a successful exit before publishing data.
            self.stop_core(require_clean_exit=False)

    def report_storage(self) -> None:
        """Publish what the multijob now holds, so the lab needs no inspection job.

        Only a report: a failure here never decides the outcome of the job.
        """
        notebook = self.c.get("notebook_root")
        roots = [self.root, self.index, *([Path(notebook)] if notebook else [])]
        try:
            self.publish_json(
                self.remote + "/multijob-storage.json", usage_report(self.stage, roots, run_id=self.run_id)
            )
        except (OSError, subprocess.SubprocessError, S5Error) as error:
            print(f"Storage report failed: {error}", file=sys.stderr)

    def publish_logs(self) -> None:
        for path in sorted(self.logs.glob("*.log")):
            self.storage.copy(path, f"{self.remote}/logs/{self.run_id}/{path.name}")
