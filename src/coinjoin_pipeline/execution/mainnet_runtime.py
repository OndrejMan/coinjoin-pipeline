"""Self-contained PBS worker. Embedded in qsub scripts; stdlib only on the host.

One synchronization is six jobs: restore, producer, blocksci, checkpoint, pack and clear. Working
data are downloaded only by the restore job and only into an empty working dataset.
Each backup has one place: the old archive is deleted, the new one streamed and its size
checked, and manifest.json, written last, makes it valid again.
"""

from __future__ import annotations

import hashlib
import fcntl
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import shlex
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import IO, TYPE_CHECKING, TypedDict

if TYPE_CHECKING:  # TypeGuard needs Python 3.10; the PBS node only evaluates no annotation.
    from typing import TypeGuard

try:
    from coinjoin_pipeline.execution.pbs.index_store import IndexStore
    from coinjoin_pipeline.execution.pbs.index_store import inventory as index_inventory
    from coinjoin_pipeline.storage.s5client import S5Client, S5Error
except ImportError:  # the PBS job ships index_store.py and s5client.py next to this script
    from index_store import IndexStore  # type: ignore[no-redef]
    from index_store import inventory as index_inventory  # type: ignore[no-redef]
    from s5client import S5Client, S5Error  # type: ignore[no-redef]

# Processes span methods (Core) or concurrent pipe streams; cleanup/finally below
# own their lifetimes rather than a single local with block.
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
    measure_download: bool
    walltime_seconds: int
    clear_after_sync: bool
    # restore and blocksci write the index, the checkpoint reads it; producer leaves it alone.
    index_lock: str | None
    index_lock_seconds: int


CHUNK = 8 * 1024 * 1024
# The parser's resume point; its bytes tie an index to one Core block directory.
RESUME_CHECKPOINT = "parsed/parser/blockList.dat.resume.json"
BLOCK_FILE = re.compile(r"blk[0-9]{5}\.dat")
# The archive is read rarely and packed once, so the level favours size over time.
PACK_LEVEL = 19
PACK_RESERVE_SECONDS = 1800  # at most a quarter of the walltime, for short test budgets


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> JsonObject:
    return json.loads(path.read_text())


def is_count(value: object) -> TypeGuard[int]:
    """A plain non-negative integer; JSON booleans do not count."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def file_sizes(root: Path) -> dict[str, int]:
    """Relative path -> size of every regular file below root."""
    return {str(path.relative_to(root)): path.stat().st_size for path in sorted(root.rglob("*")) if path.is_file()}


def megabytes_per_second(size: int, seconds: float) -> float:
    return round(size / 1e6 / max(seconds, 0.001), 1)


def pipe(stream: IO[bytes] | None) -> IO[bytes]:
    """The pipe a process was started with; every caller asked for one."""
    if stream is None:
        raise RuntimeError("The process has no pipe to stream through")
    return stream


def terminate(processes: Iterable[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
            process.wait()


def new_staging(destination: Path) -> Path:
    """An empty directory next to destination, which must not exist yet."""
    if destination.exists():
        raise ValueError("Restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=destination.name + ".restoring-", dir=destination.parent))


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def require_ready(ready: JsonObject, run_id: str) -> int:
    if ready.get("run_id") != run_id or ready.get("state") != "ready":
        raise ValueError("Missing ready checkpoint for this exact generation")
    height = ready.get("height")
    if not is_count(height) or not is_sha256(ready.get("hash")):
        raise ValueError("Invalid ready height/hash")
    if ready.get("dumplings_height") != height or not ready.get("dumplings_backup"):
        raise ValueError("Dumplings has not completed and backed up this generation")
    return height


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
    # Same POSIX record-lock handshake as the archive snapshot branch. This also
    # prevents an independently started Core from opening the datadir mid-backup.
    with (datadir / ".lock").open("rb") as handle:
        fcntl.lockf(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if (datadir / ".cookie").exists():
            raise ValueError("Core still has an RPC cookie; checkpoint refused")
        yield


def tree_bytes(root: Path, members: Sequence[str]) -> dict[str, int]:
    """Apparent size of each backup member, as tar reads it; links are not followed."""
    sizes = {}
    for member in members:
        path = root / member
        if path.is_file():
            sizes[member] = path.stat().st_size
            continue
        total = 0
        for directory, _, files in os.walk(path):
            total += sum(os.lstat(os.path.join(directory, name)).st_size for name in files)
        sizes[member] = total
    return sizes


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


# The resume bytes of the parser, recorded in the index manifest and the Core checkpoint.
SIGNATURE_KEYS = ("resume_file_number", "resume_file_prefix_size", "resume_file_prefix_sha256")


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


def container_bind(path: str | Path, readonly: bool = False) -> list[str]:
    path = Path(path)
    source = path.resolve()
    suffix = ":ro" if readonly else ""
    # MetaCentrum's /scratch/multijob-* aliases point outside /scratch. Bind
    # their targets too, so aliases resolve inside the container as on the host.
    destinations = dict.fromkeys((source, path))
    return [argument for destination in destinations for argument in ("--bind", f"{source}:{destination}{suffix}")]


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
        self.container = shutil.which("apptainer") or shutil.which("singularity")

    def publish_json(self, uri: str, value: object) -> None:
        path = self.work / "publish.json"
        atomic_json(path, value)
        self.storage.copy(path, uri)

    def verify_remote_size(self, uri: str, expected_size: int) -> None:
        size = self.storage.object_size(uri, timeout=60)
        if not is_count(size) or size != expected_size:
            raise ValueError("Checkpoint S3 size/key mismatch: " + uri)

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
        self.publish_json(f"{self.remote}/.pbs/mainnet-{self.stage}.done", value)
        self.release_dataset()

    def index_matches_inventory(self) -> bool:
        return (self.index / "files.txt").read_text() == index_inventory(self.index)

    def reset_state(self) -> None:
        """Clear the previous synchronization's state; its logs stay under logs/<run-id>/."""
        stages = ("restore", "producer", "blocksci", "checkpoint", "pack", "clear")
        # <stage>.json is each job's progress; restore.json also says what the restore did.
        names = ("ready.json", "done.json", "blocksci-index-manifest.json", *(f"{stage}.json" for stage in stages))
        for name in (*names, *(f"{stage}.quiescent" for stage in stages if stage != self.stage)):
            (self.run / name).unlink(missing_ok=True)
        for name in names:
            self.storage.remove(f"{self.remote}/{name}")
        for stage in stages:
            for outcome in ("done", "failed"):
                self.storage.remove(f"{self.remote}/.pbs/mainnet-{stage}.{outcome}")

    def container_command(
        self,
        image: str,
        *args: object,
        cwd: Path | None = None,
        readonly: bool = False,
        index_writable: bool = False,
        environment: dict[str, str] | None = None,
    ) -> list[str]:
        if self.container is None:
            raise RuntimeError("Apptainer or Singularity is required on the PBS node")
        command = [
            self.container,
            "exec",
            "--cleanenv",
            *container_bind(self.root, readonly),
            *container_bind(self.work),
        ]
        if self.index.exists() and image == self.c["blocksci_image"]:
            command += container_bind(self.index, not index_writable)
        if cwd is not None:
            command += ["--pwd", str(cwd)]
        for key, value in (environment or {}).items():
            command += ["--env", f"{key}={value}"]
        return [*command, image, *map(str, args)]

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
        )
        # Credentials are bound only to the archiver, never copied into the dataset.
        command[command.index(self.c["core_image"]) : command.index(self.c["core_image"])] = container_bind(
            self.c["credentials"], readonly=True
        )
        if prune:
            ceiling = self.work / "prune-ceiling.txt"
            ceiling.write_text(str(max(0, height - self.c["reorg_reserve"])))
            command += ["--prune-only", "--prune-ceiling-file", str(ceiling), "--prune-retain-from-file", str(number)]
        self.run_command(command, "pruning" if prune else "archiver")

    def backup(self, root: Path, members: list[str], prefix: str, metadata: JsonObject, name: str) -> str:
        """Replace the backup under prefix with a tar/zstd stream, hashing locally.

        S3 never holds two copies: the manifest and the old archive go first, so until the
        new manifest is written there is no valid backup. Only the object size is checked;
        restore verifies the checksum.
        """
        prefix = prefix.rstrip("/")
        archive = prefix + "/" + name
        manifest_uri = prefix + "/manifest.json"
        for member in members:
            path = root / member
            if not path.exists() or path.is_symlink():
                raise ValueError("Missing or linked checkpoint root: " + str(path))
        member_bytes = tree_bytes(root, members)
        self.storage.remove(manifest_uri)
        self.storage.remove(archive)
        try:
            checksum, archive_size = self.storage.upload_archive(
                root, members, archive, self.c["pbs_resources"]["ncpus"]
            )
        except S5Error as error:
            raise RuntimeError("Checkpoint stream failed") from error
        self.verify_remote_size(archive, archive_size)
        manifest = {
            **metadata,
            "run_id": self.run_id,
            "archive_uri": archive,
            "archive_sha256": checksum,
            "archive_size": archive_size,
            "members": members,
            "member_bytes": member_bytes,
            "upload_validation": "size-only",
            "uploaded_at": utc_now(),
        }
        self.publish_json(manifest_uri, manifest)
        self.uploaded[manifest_uri] = {"archive_size": archive_size, "member_bytes": member_bytes}
        return manifest_uri

    def restore_checkpoint(self, manifest_uri: str, destination: Path) -> None:
        """Explicit recovery into a new directory; never overwrite a working copy."""
        manifest = self.storage.cat_json(manifest_uri)
        archive, expected = manifest["archive_uri"], manifest["archive_sha256"]
        if not is_sha256(expected) or not archive.startswith(manifest_uri.rsplit("/", 1)[0] + "/"):
            raise ValueError("Invalid checkpoint reference")
        members = manifest["members"]
        if (
            not isinstance(members, list)
            or not members
            or any(not isinstance(name, str) or name.startswith("/") or ".." in Path(name).parts for name in members)
        ):
            raise ValueError("Invalid checkpoint member roots")
        staging = new_staging(destination)
        (staging / ".restoring").touch()
        digest = hashlib.sha256()
        errors: list[Exception] = []
        source = self.storage.popen("cat", archive, stdout=subprocess.PIPE)
        decompress = subprocess.Popen(["zstd", "-d"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        downloaded, compressed, archive_stream = pipe(source.stdout), pipe(decompress.stdin), pipe(decompress.stdout)

        def pump() -> None:
            try:
                for chunk in iter(lambda: downloaded.read(CHUNK), b""):
                    digest.update(chunk)
                    compressed.write(chunk)
            except Exception as error:
                errors.append(error)
            finally:
                compressed.close()

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            with tarfile.open(fileobj=archive_stream, mode="r|") as stream:
                for member in stream:
                    relative = Path(member.name)
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or not any(relative == Path(root) or Path(root) in relative.parents for root in members)
                    ):
                        raise ValueError("Checkpoint contains a path outside its member roots")
                    output = staging / relative
                    if member.isdir():
                        output.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        output.parent.mkdir(parents=True, exist_ok=True)
                        with pipe(stream.extractfile(member)) as handle, output.open("wb") as target:
                            shutil.copyfileobj(handle, target)
                    else:
                        raise ValueError("Checkpoint contains a link or special file")
            # Drain trailing tar padding, allowing the decompressor and pump to finish.
            while archive_stream.read(CHUNK):
                pass
            thread.join(timeout=30)
            if (
                thread.is_alive()
                or source.wait() != 0
                or decompress.wait() != 0
                or errors
                or digest.hexdigest() != expected
            ):
                raise ValueError("Checkpoint restore SHA-256/stream verification failed")
            if any(not (staging / member).exists() for member in members):
                raise ValueError("Checkpoint is missing a declared member")
            (staging / ".restoring").unlink()
            staging.rename(destination)
        finally:
            terminate((source, decompress))
            thread.join(timeout=5)

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

    def restore_dumplings(self, manifest_uri: str, destination: Path) -> None:
        """Explicit recovery of Scanner/ into a new directory, checked against the manifest."""
        files = self.storage.cat_json(manifest_uri).get("files")
        if (
            not isinstance(files, dict)
            or not files
            or any(name.startswith("/") or ".." in Path(name).parts for name in files)
        ):
            raise ValueError("Invalid Dumplings manifest")
        staging = new_staging(destination)
        try:
            self.storage.copy(manifest_uri.rsplit("/", 1)[0] + "/Scanner/*", str(staging) + "/", timeout=6 * 3600)
            if file_sizes(staging) != files:
                raise ValueError("The restored Scanner/ does not match its manifest")
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

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

    def dataset_members(self) -> list[Path]:
        """Working data that exist; locks, synchronization state and logs do not count."""
        index = (self.index / name for name in ("config.json", "files.txt", "manifest.json", "parsed"))
        return [path for path in (self.datadir, self.dumps, self.archive_state, *index) if path.exists()]

    def measured(self, name: str, action: Callable[[], None], downloaded: int, target: Path) -> JsonObject:
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

    def raw_download(self, name: str, uri: str, size: int) -> JsonObject:
        """Read an archive and discard it: the S3 speed without unpacking or disk writes."""
        started = time.monotonic()
        process = self.storage.popen("cat", uri, stdout=subprocess.DEVNULL)
        try:
            if process.wait() != 0:
                raise ValueError("Could not read the archive: " + uri)
        finally:
            terminate((process,))
        seconds = time.monotonic() - started
        raw = {"raw_download_seconds": round(seconds, 1), "raw_download_mb_per_s": megabytes_per_second(size, seconds)}
        print(f"raw download {name}: {json.dumps(raw)}", flush=True)
        return raw

    def restore_dataset(self) -> None:
        """Job 0: fill an empty working dataset from the S3 backups, otherwise do nothing.

        The working copy on multijob storage can expire. Only a completely empty one is
        restored, from three backups of one synchronization; a partial one is never mixed
        with backups, and the producer refuses it.
        """
        self.stage = "restore"
        self.acquire_dataset()
        self.reset_state()
        # Only a killed restore leaves these; both locks are held, so none is in use.
        leftovers = (".restore-*", ".clear-*")
        for leftover in (path for base in (self.root, self.index) for name in leftovers for path in base.glob(name)):
            shutil.rmtree(leftover)
        present = self.dataset_members()
        if present:
            self.finish_restore("skipped-not-empty", present=[str(path) for path in present])
            return
        backups = {
            name: prefix.rstrip("/") + "/manifest.json"
            for name, prefix in (
                ("core", self.c["backup_uri"]),
                ("index", self.c["index_backup_uri"]),
                ("dumplings", self.c["dumplings_uri"]),
            )
        }
        self.progress("restore-check")
        manifests = {
            name: self.storage.cat_json(uri, timeout=120) if self.storage.exists(uri) else None
            for name, uri in backups.items()
        }
        found = {name: value for name, value in manifests.items() if value is not None}
        missing = sorted(backups.keys() - found.keys())
        if not found:
            if self.storage.listing(self.c["blocks_uri"].rstrip("/") + "/*"):
                raise ValueError(
                    "S3 holds archived blocks but no backups; a new initial download would mix its block files "
                    "with the archived ones. Restore the backups or empty " + self.c["blocks_uri"]
                )
            self.finish_restore("skipped-no-backup")
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
        started = time.monotonic()
        raw = {}
        if self.c.get("measure_download"):
            self.progress("raw-download", height=core["height"])
            raw = {
                name: self.raw_download(name, value["archive_uri"], value["archive_size"])
                for name, value in (("core", core), ("index", index))
            }
        self.progress("restore-download", height=core["height"])
        staging = self.root / f".restore-{self.run_id}"
        index_staging = self.index / f".restore-{self.run_id}"
        try:
            staging.mkdir()
            index_staging.mkdir(parents=True)
            parts = {
                "core": self.measured(
                    "core",
                    lambda: self.restore_checkpoint(backups["core"], staging / "core"),
                    core["archive_size"],
                    staging / "core",
                ),
                "dumplings": self.measured(
                    "dumplings",
                    lambda: self.restore_dumplings(backups["dumplings"], staging / "Scanner"),
                    sum(dumplings["files"].values()),
                    staging / "Scanner",
                ),
                "index": self.measured(
                    "index",
                    lambda: self.restore_checkpoint(backups["index"], index_staging / "index"),
                    index["archive_size"],
                    index_staging / "index",
                ),
            }
            # Every backup is downloaded and verified; only now does any of it take its place.
            for entry in (staging / "core").iterdir():
                entry.rename(self.root / entry.name)
            self.dumps.mkdir()
            (staging / "Scanner").rename(self.dumps / "Scanner")
            for entry in (index_staging / "index").iterdir():
                entry.rename(self.index / entry.name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(index_staging, ignore_errors=True)
        if not self.index_matches_inventory():
            raise ValueError("The restored index does not match its file inventory")
        if resume_signature(self.index, self.core_dir / "blocks") != {key: core[key] for key in SIGNATURE_KEYS}:
            raise ValueError("The restored index and Core blocks do not belong together")
        self.finish_restore(
            "restored",
            height=core["height"],
            hash=core["hash"],
            backups=backups,
            parts={name: {**part, **raw.get(name, {})} for name, part in parts.items()},
            total_seconds=round(time.monotonic() - started, 1),
            host=socket.gethostname(),
        )

    def finish_restore(self, action: str, **extra: object) -> None:
        self.progress("complete", None, None, "done", action=action, **extra)
        self.complete({"run_id": self.run_id, "action": action, **extra})

    def require_complete_dataset(self) -> None:
        """Refuse a partly lost working dataset before Core starts.

        The restore job fills only an empty dataset. An unfinished first synchronization
        (unpruned blocks, no index yet) continues; anything else that is missing is an error.
        """
        present = {
            "Core chainstate": (self.core_dir / "chainstate").is_dir(),
            "Core blocks": (self.core_dir / "blocks").is_dir(),
            "block archive state": self.archive_state.is_dir(),
            "Dumplings Scanner": (self.dumps / "Scanner/LastProcessedBlockHeight.txt").is_file(),
            "BlockSci index": (self.index / "config.json").is_file(),
        }
        if not any(present.values()):
            try:
                restore = read_json(self.run / "restore.json")
            except (OSError, ValueError):
                restore = {}
            if restore.get("run_id") != self.run_id or restore.get("action") != "skipped-no-backup":
                raise ValueError("The working dataset is empty but this synchronization's restore job did not check S3")
            return
        missing = [name for name, found in present.items() if not found]
        unfinished_first = not present["BlockSci index"] and (self.core_dir / "blocks/blk00000.dat").is_file()
        if missing and not unfinished_first:
            raise ValueError(
                "The working dataset is incomplete (missing: " + ", ".join(missing) + "). Remove "
                f"{self.root} and {self.index} entirely; the next synchronization restores both from S3."
            )

    def producer(self) -> None:
        self.stage = "producer"
        self.acquire_dataset()
        self.require_complete_dataset()
        atomic_json(self.root / "current-ready.json", {"run_id": self.run_id, "state": "preparing"})
        self.progress("core-startup")
        self.start_core(False)
        info = self.synchronize_core()
        height, block_hash = info["blocks"], info["bestblockhash"]
        self.dumps.mkdir(parents=True, exist_ok=True)
        scan_height = self.dumps / "Scanner/LastProcessedBlockHeight.txt"
        start = int(scan_height.read_text()) + 1 if scan_height.exists() else self.scan_start
        if info.get("pruneheight", 0) > start:
            raise ValueError("Dumplings needs blocks already pruned from this working copy; restore them first")
        scanner = self.dumps / "Scanner"
        anchor_path = scanner / "ChainCheckpoint.json"
        if scan_height.exists():
            anchor = read_json(anchor_path)
            anchor_height = anchor.get("height")
            if not is_count(anchor_height) or not start - 1 <= anchor_height <= height:
                raise ValueError("Dumplings chain anchor does not cover its saved progress")
            if self.rpc("getblockhash", str(anchor_height)) != anchor.get("hash"):
                raise ValueError("Dumplings checkpoint is on a different fork; explicit recovery required")
        # Bind even a partially completed scan to this frozen chain. A retry can
        # resume C# checkpoints, but never silently carry detections across a reorg.
        scanner.mkdir(parents=True, exist_ok=True)
        atomic_json(anchor_path, {"height": height, "hash": block_hash})

        def scan_progress() -> None:
            try:
                current = int(scan_height.read_text())
            except (OSError, ValueError):
                current = self.scan_start - 1
            self.progress(
                "dumplings-scan",
                max(0, current - self.scan_start + 1),
                max(0, height - self.scan_start + 1),
                height=height,
                scanner_height=current,
                target_height=height,
            )

        self.run_command(
            self.container_command(
                self.c["dumplings_image"],
                "dotnet",
                "/app/Dumplings.Cli.dll",
                "sync",
                "--include-tip",
                f"--frozen-height={height}",
                f"--rpccookiefile={self.core_dir / '.cookie'}",
                f"--rpcurl=http://127.0.0.1:{self.port}/",
                f"--network={self.chain}",
                *([f"--startheight={self.scan_start}"] if self.chain == "regtest" else []),
                cwd=self.dumps,
            ),
            "dumplings",
            scan_progress,
        )
        scanner_complete(scanner, height)
        self.progress("block-archive")
        self.archive()
        dumps_backup = self.publish_dumplings(height)
        if self.rpc("getblockhash", str(height)) != block_hash or self.rpc("getblockcount") != height:
            raise ValueError("Core changed while preparing the generation")
        self.stop_core()
        ready = {
            "run_id": self.run_id,
            "state": "ready",
            "height": height,
            "hash": block_hash,
            "dumplings_height": height,
            "dumplings_backup": dumps_backup,
            "source_dir": str(self.core_dir),
            "network": self.network,
            "requested_target_height": self.c.get("target_height"),
        }
        atomic_json(self.run / "ready.json", ready)
        atomic_json(self.root / "current-ready.json", ready)
        self.publish_json(self.remote + "/ready.json", ready)
        self.progress("ready-for-blocksci", state="done", height=height)
        self.complete(ready)

    def parse_progress(self) -> None:
        path = self.logs / "blocksci.log"
        if not path.exists():
            return
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 8192))
            tail = handle.read().decode("utf-8", errors="replace")
        matches = re.findall(r"(\d+(?:\.\d+)?)%([^\r\n]*)", tail)
        self.progress(
            "blocksci-parse",
            float(matches[-1][0]) if matches else None,
            100 if matches else None,
            detail=matches[-1][1].strip() if matches else None,
        )

    def blocksci(self) -> None:
        self.stage = "blocksci"
        self.acquire_dataset()
        ready = read_json(self.run / "ready.json")
        height = require_ready(ready, self.run_id)
        if ready.get("network", self.network) != self.network:
            raise ValueError("The ready generation uses a different network")
        current = read_json(self.root / "current-ready.json")
        require_ready(current, self.run_id)
        if current != ready:
            raise ValueError("The working dataset no longer matches this ready generation")
        if ready["source_dir"] != str(self.core_dir):
            raise ValueError("Ready generation has a different block source")
        self.index.mkdir(parents=True, exist_ok=True)
        marker = self.index / ".writing"
        if marker.exists():
            raise ValueError("Index is incomplete; explicitly restore the previous checkpoint before retrying")
        config = self.index / "config.json"
        previous = None
        if config.exists():
            if not self.index_matches_inventory():
                raise ValueError("Persistent index does not match its file inventory; restore it before updating")
            previous = read_json(self.index / "manifest.json")
            if (
                previous.get("source_dir") != str(self.core_dir)
                or previous.get("blocksci_image") != self.c["blocksci_image"]
            ):
                raise ValueError("Persistent index source/image changed; explicit migration required")
            signature = resume_signature(self.index, self.core_dir / "blocks")
            if any(previous.get(key) != value for key, value in signature.items()):
                raise ValueError("BlockSci resume bytes do not match this Core working copy; restore the matching pair")
        elif (self.index / "parsed").exists():
            raise ValueError("Index has data but no configuration; refusing to overwrite it")
        elif not (self.core_dir / "blocks/blk00000.dat").is_file():
            raise ValueError("Initial BlockSci parse requires the full block history; restore it explicitly")
        marker.touch()
        self.progress("blocksci-config")
        image = self.c["blocksci_image"]
        if not config.exists():
            self.run_command(
                self.container_command(
                    image,
                    "blocksci_parser",
                    config,
                    "generate-config",
                    self.network,
                    self.index / "parsed",
                    "--disk",
                    self.core_dir,
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
        (self.index / RESUME_CHECKPOINT).unlink(missing_ok=True)
        self.run_command(
            self.container_command(image, "blocksci_parser", config, "update", readonly=True, index_writable=True),
            "blocksci",
            self.parse_progress,
        )
        signature = resume_signature(self.index, self.core_dir / "blocks")
        output = self.work / "verified.json"
        self.progress("blocksci-verify")
        verify = [
            "/usr/bin/python3",
            str(self.work / "verify_chain.py"),
            str(config),
            "--max-block",
            str(height),
            "--network",
            self.network,
            "--expected-block-hash",
            ready["hash"],
            "--output",
            str(output),
        ]
        if previous is not None and previous.get("exported_max_block", height) < height:
            atomic_json(self.work / "previous.json", previous)
            verify += ["--source-manifest", str(self.work / "previous.json")]
        self.run_command(
            self.container_command(
                image,
                *verify,
                readonly=True,
                environment={"PYTHONPATH": "/blocksci/.venv/lib/python3.8/site-packages:/mnt/blocksci/blockscipy"},
            ),
            "blocksci-verify",
        )
        verified = read_json(output)
        manifest = {
            "schema_version": "1.1",
            "storage": "persistent",
            "source_kind": "external-bitcoin",
            "network": self.network,
            "run_id": self.run_id,
            "source_dir": str(self.core_dir),
            "index_dir": str(self.index),
            "blocksci_image": image,
            "pbs_resources": self.c["pbs_resources"],
            **signature,
            "published_at": utc_now(),
            **verified,
        }
        atomic_json(self.index / "manifest.json", manifest)
        (self.index / "files.txt").write_text(index_inventory(self.index))
        # The index is complete; its backup is the checkpoint job's, so a failed upload
        # leaves a usable working copy.
        marker.unlink()
        self.publish_json(self.remote + "/blocksci-index-manifest.json", manifest)
        self.progress("indexed", state="done", height=height)
        self.complete(manifest)

    def checkpoint(self) -> None:
        """Job 3: back up the index, prune Core and back up its checkpoint, with little memory.

        Holds the index reader lock: notebooks keep reading, index jobs cannot write.
        """
        self.stage = "checkpoint"
        self.acquire_dataset()
        ready = read_json(self.run / "ready.json")
        height = require_ready(ready, self.run_id)
        if read_json(self.root / "current-ready.json") != ready:
            raise ValueError("The working dataset no longer matches this ready generation")
        manifest = read_json(self.index / "manifest.json")
        if (
            (self.index / ".writing").exists()
            or manifest.get("run_id") != self.run_id
            or manifest.get("exported_max_block") != height
            or not self.index_matches_inventory()
        ):
            raise ValueError("The index is not the one this synchronization's BlockSci job completed")
        number = resume_file(self.index, self.core_dir / "blocks")
        signature = resume_signature(self.index, self.core_dir / "blocks")
        if any(manifest.get(key) != value for key, value in signature.items()):
            raise ValueError("BlockSci resume bytes no longer match this Core working copy")
        self.progress("index-backup")
        index_backup = self.backup(
            self.index,
            ["config.json", "files.txt", "manifest.json", "parsed"],
            self.c["index_backup_uri"],
            manifest,
            "blocksci_index.tar.zst",
        )
        self.progress("pruning")
        # Offline Core only loads its chain for pruning; a large cache would just cost memory.
        info = self.start_core(True, dbcache_mb=min(1024, self.c["dbcache_mb"]))
        if info["blocks"] != height or info["bestblockhash"] != ready["hash"]:
            raise ValueError("Offline Core does not match the ready generation")
        self.archive(prune=True, height=min(height, ready["dumplings_height"]), number=number)
        self.stop_core()
        if resume_signature(self.index, self.core_dir / "blocks") != signature:
            raise ValueError("Core cleanup changed the BlockSci resume source; checkpoint refused")
        self.progress("core-checkpoint")
        with stopped_core(self.core_dir):
            checkpoint = self.backup(
                self.root,
                [
                    str((self.core_dir / "blocks").relative_to(self.root)),
                    str((self.core_dir / "chainstate").relative_to(self.root)),
                    # Scanner/ is restored from its own incremental copy (publish_dumplings).
                    "archive-state",
                ],
                self.c["backup_uri"],
                {"height": height, "hash": ready["hash"], "index_backup": index_backup, **signature},
                "checkpoint.tar.zst",
            )
        done = {
            **ready,
            "state": "done",
            "core_checkpoint": checkpoint,
            "index_backup": index_backup,
            # Measured while Core is stopped and no index job can write.
            "storage": {
                "measured_at": utc_now(),
                "multijob": {
                    "core": self.uploaded.get(checkpoint, {}).get("member_bytes"),
                    "index": self.uploaded.get(index_backup, {}).get("member_bytes"),
                },
                "s3": {
                    "core_checkpoint": self.uploaded.get(checkpoint, {}).get("archive_size"),
                    "index_backup": self.uploaded.get(index_backup, {}).get("archive_size"),
                },
            },
        }
        self.progress("complete", state="done", height=height)
        self.publish_json(self.remote + "/done.json", done)
        self.complete(done)
        atomic_json(self.run / "done.json", done)

    def pack_block(self, name: str, work: Path) -> tuple[int, int]:
        """Replace one pruned raw block file in S3 by a verified .zst; safe to repeat.

        The raw object goes only after the uploaded archive was read back and its content
        matched the SHA-256 the archiver recorded in the sidecar, which stays unchanged.
        """
        raw = self.c["blocks_uri"].rstrip("/") + "/" + name
        packed = raw + ".zst"
        sidecar = self.storage.cat_json(raw + ".json")
        expected, size = sidecar.get("sha256"), sidecar.get("size")
        if not is_sha256(expected) or not is_count(size):
            raise ValueError("Invalid block sidecar: " + name)
        local = work / name
        self.storage.copy(raw, str(local))
        try:
            digest = hashlib.sha256()
            with local.open("rb") as stream:
                for chunk in iter(lambda: stream.read(CHUNK), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected or local.stat().st_size != size:
                raise ValueError("Raw block object does not match its sidecar: " + name)
            archive = work / (name + ".zst")
            subprocess.run(["zstd", "-q", "-f", f"-{PACK_LEVEL}", "-T1", str(local), "-o", str(archive)], check=True)
            compressed = archive.stat().st_size
            self.storage.copy(str(archive), packed)
        finally:
            local.unlink(missing_ok=True)
            (work / (name + ".zst")).unlink(missing_ok=True)
        self.verify_remote_size(packed, compressed)
        source = self.storage.popen("cat", packed, stdout=subprocess.PIPE)
        unpack = subprocess.Popen(["zstd", "-q", "-d", "-c"], stdin=source.stdout, stdout=subprocess.PIPE)
        pipe(source.stdout).close()
        digest = hashlib.sha256()
        try:
            for chunk in iter(lambda: pipe(unpack.stdout).read(CHUNK), b""):
                digest.update(chunk)
            if unpack.wait() != 0 or source.wait() != 0 or digest.hexdigest() != expected:
                raise ValueError("Packed block does not unpack to its sidecar checksum: " + name)
        finally:
            terminate((source, unpack))
        if not self.storage.remove(raw) or self.storage.exists(raw):
            raise ValueError("Raw block object was not removed: " + name)
        return size, compressed

    def pack(self) -> None:
        """Job 4: compress the archive's block files that Core no longer keeps.

        Takes no dataset lock, so the next synchronization may start. Only files already
        pruned from disk qualify: pruning verifies the raw object of every file on disk.
        S3 is listed first, so a file uploaded meanwhile is seen on disk and skipped.
        """
        self.stage = "pack"
        started = time.monotonic()
        if read_json(self.run / "done.json").get("run_id") != self.run_id:
            raise ValueError("This synchronization's checkpoint did not finish; packing refused")
        listed = self.storage.listing(self.c["blocks_uri"].rstrip("/") + "/*", timeout=None)
        raw = sorted(
            name for name in (str(entry["key"]).rsplit("/", 1)[-1] for entry in listed) if BLOCK_FILE.fullmatch(name)
        )
        on_disk = {path.name for path in (self.core_dir / "blocks").glob("blk*.dat")}
        if not on_disk:
            raise ValueError("Core has no block files on disk; packing refused")
        pending = [name for name in raw if name not in on_disk]
        # Whatever is left stays raw for the next run; a running file takes a few minutes.
        walltime = self.c["walltime_seconds"]
        deadline = started + walltime - min(PACK_RESERVE_SECONDS, walltime // 4)
        stop = threading.Event()
        lock = threading.Lock()
        totals = {"packed": 0, "raw_bytes": 0, "packed_bytes": 0}
        failed: list[str] = []
        self.progress("packing", 0, len(pending))

        def work(name: str) -> None:
            if stop.is_set() or time.monotonic() > deadline:
                return
            directory = Path(tempfile.mkdtemp(dir=self.work))
            try:
                before, after = self.pack_block(name, directory)
            except (ValueError, OSError, subprocess.SubprocessError, S5Error) as error:
                print(f"Packing {name} failed: {error}", file=sys.stderr, flush=True)
                with lock:
                    failed.append(name)
                return
            finally:
                shutil.rmtree(directory, ignore_errors=True)
            with lock:
                totals["packed"] += 1
                totals["raw_bytes"] += before
                totals["packed_bytes"] += after
                done = totals["packed"]
            if done % 20 == 0:
                self.progress("packing", done, len(pending))

        try:
            with ThreadPoolExecutor(self.c["pbs_resources"]["ncpus"]) as pool:
                list(pool.map(work, pending))
        except BaseException:
            stop.set()
            raise
        summary = {
            **totals,
            "failed": failed,
            "remaining": len(pending) - totals["packed"],
            "seconds": round(time.monotonic() - started, 1),
        }
        print("pack summary: " + json.dumps(summary), flush=True)
        if failed:
            raise ValueError(f"{len(failed)} block files could not be packed; they stay raw in S3")
        self.progress("complete", totals["packed"], len(pending), "done", **summary)
        self.complete({"run_id": self.run_id, **summary})

    def clear(self) -> None:
        """Job 5: empty the working dataset; the next restore job refills it from S3.

        Everything moves aside first, so a killed job never leaves a partial dataset that
        the producer would refuse; the restore job removes such leftovers.
        """
        self.stage = "clear"
        self.acquire_dataset()
        if read_json(self.run / "done.json").get("run_id") != self.run_id:
            raise ValueError("This synchronization's checkpoint did not finish; clearing refused")
        self.progress("clearing")
        trash = self.root / f".clear-{self.run_id}"
        index_trash = self.index / f".clear-{self.run_id}"
        trash.mkdir()
        index_trash.mkdir()
        for path in self.dataset_members():
            path.rename((index_trash if path.parent == self.index else trash) / path.name)
        (self.root / "current-ready.json").unlink(missing_ok=True)
        shutil.rmtree(trash)
        shutil.rmtree(index_trash)
        self.progress("complete", state="done")
        self.complete({"run_id": self.run_id})

    def cleanup(self) -> None:
        for process in self.children:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=30)
        if self.core is not None:
            # Cleanup needs proof of process termination; checkpoint callers
            # additionally require a successful exit before publishing data.
            self.stop_core(require_clean_exit=False)

    def publish_logs(self) -> None:
        for path in sorted(self.logs.glob("*.log")):
            self.storage.copy(path, f"{self.remote}/logs/{self.run_id}/{path.name}")


def main(argv: list[str] | None = None) -> None:
    args = argv or sys.argv[1:]
    settings = Path(args[0])
    worker = Worker(json.loads(settings.read_text(encoding="utf-8")), settings.parent)
    # Explicit recovery: restore <manifest> <new directory>; the restore job takes no arguments.
    if args[1] == "restore" and len(args) == 4:
        worker.restore_checkpoint(args[2], Path(args[3]))
        return
    if args[1] == "restore-dumplings":
        worker.restore_dumplings(args[2], Path(args[3]))
        return
    if args[1] == "failed":
        worker.stage = args[2].removeprefix("mainnet-")
        if (len(args) > 3 and args[3] == "0") or (worker.run / f"{worker.stage}.quiescent").is_file():
            worker.release_dataset()
        worker.progress("failed", state="failed", error="PBS stage failed; inspect the persistent job logs")
        worker.publish_json(worker.remote + f"/.pbs/mainnet-{worker.stage}.failed", {"run_id": worker.run_id})
        return
    worker.stage = args[1]
    job = worker.restore_dataset if args[1] == "restore" else getattr(worker, args[1])
    # The fixed state directory may hold the marker of an earlier run of this stage.
    (worker.run / f"{worker.stage}.quiescent").unlink(missing_ok=True)

    def interrupted(_signum: int, _frame: object) -> None:
        raise InterruptedError("PBS job interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    # Fail at once on an active notebook or index job instead of holding an allocation waiting.
    index = IndexStore(
        worker.c["index_dir"], None, lock_seconds=worker.c.get("index_lock_seconds", 0), poll_seconds=1
    )
    try:
        mode = worker.c.get("index_lock")
        if mode == "exclusive":
            index.lock_exclusive()
        elif mode == "shared":
            index.lock_shared()
        job()
    finally:
        index.unlock()
        try:
            worker.cleanup()
            worker.run.mkdir(parents=True, exist_ok=True)
            (worker.run / f"{worker.stage}.quiescent").touch()
        finally:
            try:
                worker.publish_logs()
            except Exception as error:
                print(f"Log upload failed; logs remain in {worker.run}: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
