"""Self-contained PBS worker. Embedded in qsub scripts; stdlib only on the host.

Working data are never downloaded implicitly. Recovery is an explicit operation.
Backups use immutable generation prefixes and switch latest.json only after verification.
"""

from __future__ import annotations

import hashlib
import fcntl
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
from datetime import datetime, timezone
from contextlib import contextmanager

# Processes span methods (Core) or concurrent pipe streams; cleanup/finally below
# own their lifetimes rather than a single local with block.
# pylint: disable=consider-using-with


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def digest_stream(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def require_ready(ready, run_id):
    if ready.get("run_id") != run_id or ready.get("state") != "ready":
        raise ValueError("Missing ready checkpoint for this exact generation")
    height = ready.get("height")
    if (
        not isinstance(height, int)
        or isinstance(height, bool)
        or height < 0
        or not re.fullmatch(r"[0-9a-f]{64}", ready.get("hash", ""))
    ):
        raise ValueError("Invalid ready height/hash")
    if ready.get("dumplings_height") != height or not ready.get("dumplings_backup"):
        raise ValueError("Dumplings has not completed and backed up this generation")
    return height


def scanner_complete(scanner, height):
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
    anchor = json.loads((scanner / "ChainCheckpoint.json").read_text())
    if anchor.get("height") != height or not re.fullmatch(r"[0-9a-f]{64}", anchor.get("hash", "")):
        raise ValueError("Dumplings checkpoint has no matching chain anchor")


@contextmanager
def stopped_core(datadir):
    # Same POSIX record-lock handshake as the archive snapshot branch. This also
    # prevents an independently started Core from opening the datadir mid-backup.
    with (datadir / ".lock").open("rb") as handle:
        fcntl.lockf(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if (datadir / ".cookie").exists():
            raise ValueError("Core still has an RPC cookie; checkpoint refused")
        yield


def resume_file(index, blocks):
    path = index / "parsed/parser/blockList.dat.resume.json"
    checkpoint = json.loads(path.read_text())
    number = checkpoint.get("file_number")
    if checkpoint.get("schema_version") != 1 or not isinstance(number, int) or isinstance(number, bool) or number < 0:
        raise ValueError("Invalid BlockSci parser resume checkpoint")
    source = blocks / f"blk{number:05d}.dat"
    offset, size = checkpoint.get("data_offset"), checkpoint.get("block_size")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or offset < 8
        or size < 80
    ):
        raise ValueError("Invalid BlockSci parser resume offset")
    if not source.is_file() or source.stat().st_size < offset + size:
        raise ValueError("BlockSci parser resume file is missing or truncated")
    return number


def index_inventory(index):
    """Match index_store.sh's inventory so existing notebook readers accept it."""
    rows = []
    for path in index.rglob("*"):
        relative = path.relative_to(index)
        if relative.parts[0] in {".lock", ".writing"} or not path.is_file():
            continue
        if path.name in {"files.txt", "manifest.json"} or path.name.endswith(".tmp"):
            continue
        rows.append(f"{relative} {path.stat().st_size}")
    return "\n".join(sorted(rows)) + "\n"


def resume_signature(index, blocks):
    number = resume_file(index, blocks)
    checkpoint = json.loads((index / "parsed/parser/blockList.dat.resume.json").read_text())
    end = checkpoint["data_offset"] + checkpoint["block_size"]
    remaining, digest = end, hashlib.sha256()
    with (blocks / f"blk{number:05d}.dat").open("rb") as handle:
        while remaining:
            chunk = handle.read(min(remaining, 8 * 1024 * 1024))
            if not chunk:
                raise ValueError("Parser resume source was truncated while hashing")
            remaining -= len(chunk)
            digest.update(chunk)
    return {
        "resume_file_number": number,
        "resume_file_prefix_size": end,
        "resume_file_prefix_sha256": digest.hexdigest(),
    }


def container_bind(path, readonly=False):
    path = Path(path)
    source = path.resolve()
    suffix = ":ro" if readonly else ""
    # MetaCentrum's /scratch/multijob-* aliases point outside /scratch. Bind
    # their targets too, so aliases resolve inside the container as on the host.
    destinations = dict.fromkeys((source, path))
    return [argument for destination in destinations for argument in ("--bind", f"{source}:{destination}{suffix}")]


class Worker:
    def __init__(self, settings, work):
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
        self.run = self.root / "runs" / self.run_id
        self.active = self.root / ".sync-active"
        self.remote = settings["artifact_uri"].rstrip("/") + "/" + self.run_id
        self.s5 = [
            "s5cmd",
            "--credentials-file",
            settings["credentials"],
            "--profile",
            settings["profile"],
            "--endpoint-url",
            settings["endpoint"],
        ]
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
        self.core = None
        self.children = []
        self.port = None
        self.stage = ""
        self.container = shutil.which("apptainer") or shutil.which("singularity")

    def s3(self, *args, **kwargs):
        return subprocess.run([*self.s5, *map(str, args)], env=self.env, check=True, **kwargs)

    def upload(self, path, uri):
        self.s3("cp", path, uri, stdout=subprocess.DEVNULL, timeout=3600)

    def publish_json(self, uri, value):
        path = self.work / "publish.json"
        atomic_json(path, value)
        self.upload(path, uri)

    def remote_hash(self, uri):
        with subprocess.Popen([*self.s5, "cat", uri], env=self.env, stdout=subprocess.PIPE) as process:
            result = digest_stream(process.stdout)
            if process.wait() != 0:
                raise RuntimeError("S3 read-back failed: " + uri)
        return result

    def verify_remote_size(self, uri, expected_size):
        result = self.s3("--json", "ls", uri, stdout=subprocess.PIPE, text=True, timeout=60)
        metadata = json.loads(result.stdout)
        size = metadata.get("size")
        if metadata.get("key") != uri or not isinstance(size, int) or isinstance(size, bool) or size != expected_size:
            raise ValueError("Checkpoint S3 size/key mismatch: " + uri)

    def upload_verified(self, path, uri):
        with path.open("rb") as handle:
            expected = digest_stream(handle)
        self.upload(path, uri)
        if self.remote_hash(uri) != expected:
            raise ValueError("Uploaded object failed SHA-256 verification: " + uri)
        return expected

    def progress(self, phase, current=None, total=None, state="running", **extra):
        percent = None if total is None or current is None or total <= 0 else round(min(100, 100 * current / total), 2)
        value = {
            "run_id": self.run_id,
            "stage": self.stage,
            "phase": phase,
            "state": state,
            "current": current,
            "total": total,
            "percent": percent,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        atomic_json(self.run / f"{self.stage}.json", value)
        self.publish_json(f"{self.remote}/synchronization/{self.stage}.json", value)
        print(json.dumps(value), flush=True)

    def owns_dataset(self):
        try:
            return json.loads((self.active / "owner.json").read_text()).get("run_id") == self.run_id
        except (OSError, ValueError):
            return False

    def acquire_dataset(self):
        self.root.mkdir(parents=True, exist_ok=True)
        # mkdir is atomic on the shared filesystem. No expiry guesses across queue waits.
        self.active.mkdir()
        atomic_json(self.active / "owner.json", {"run_id": self.run_id, "job_id": os.environ.get("PBS_JOBID")})

    def release_dataset(self):
        if self.owns_dataset():
            shutil.rmtree(self.active)

    def container_command(self, image, *args, cwd=None, readonly=False, index_writable=False, environment=None):
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

    def run_command(self, command, name, poll=None):
        self.run.mkdir(parents=True, exist_ok=True)
        with (self.run / f"{name}.log").open("ab") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
            )
            self.children.append(process)
            while process.poll() is None:
                if poll:
                    poll()
                time.sleep(self.c["poll_seconds"])
            if process.returncode:
                raise RuntimeError(f"{name} exited with {process.returncode}; see {self.run / (name + '.log')}")
        return process

    def rpc(self, method, *args):
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

    def start_core(self, offline):
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
            f"-dbcache={self.c['dbcache_mb']}",
            f"-networkactive={0 if offline or self.chain == 'regtest' else 1}",
        )
        self.run.mkdir(parents=True, exist_ok=True)
        with (self.run / f"core-{self.stage}.log").open("ab") as log:
            self.core = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        while self.core.poll() is None:
            try:
                info = self.rpc("getblockchaininfo")
                if info.get("chain") != self.chain or info.get("automatic_pruning") is not False:
                    raise ValueError("Core must use the configured network and manual pruning")
                if self.rpc("getindexinfo"):
                    raise ValueError("The synchronization dataset must have txindex and optional indexes disabled")
                return info
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                self.progress("core-startup")
                time.sleep(self.c["poll_seconds"])
        raise RuntimeError("Core failed to start; see its persistent log")

    def stop_core(self, *, require_clean_exit=True):
        if self.core is None:
            return
        if self.core.poll() is None:
            self.rpc("stop")
        # A checkpoint is forbidden until bitcoind has fully flushed and exited.
        code = self.core.wait(timeout=600)
        self.core = None
        if require_clean_exit and code != 0:
            raise RuntimeError("Core did not shut down cleanly")

    def archive(self, prune=False, height=None, number=None):
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

    def backup(self, root, members, prefix, metadata):
        """Stream tar/zstd to S3, hashing locally and checking remote object size.

        Content checksum verification is deferred to restore; no archive read-back.
        Failure never moves latest.json.
        """
        generation = prefix.rstrip("/") + "/generations/" + self.run_id
        archive = generation + "/checkpoint.tar.zst"
        published = subprocess.run(
            [*self.s5, "ls", generation + "/manifest.json"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if published.returncode == 0:
            raise ValueError("Backup generation already published; choose a new run ID")
        for member in members:
            path = root / member
            if not path.exists() or path.is_symlink():
                raise ValueError("Missing or linked checkpoint root: " + str(path))
        producers = []
        digest = hashlib.sha256()
        archive_size = 0
        upload = None
        try:
            tar = subprocess.Popen(["tar", "-C", str(root), "-cf", "-", *members], stdout=subprocess.PIPE)
            producers.append(tar)
            zstd = subprocess.Popen(
                ["zstd", "-3", "-T" + str(self.c["pbs_resources"]["ncpus"])], stdin=tar.stdout, stdout=subprocess.PIPE
            )
            tar.stdout.close()
            producers.append(zstd)
            upload = subprocess.Popen(
                [*self.s5, "pipe", archive], env=self.env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
            )
            for chunk in iter(lambda: zstd.stdout.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
                archive_size += len(chunk)
                upload.stdin.write(chunk)
            upload.stdin.close()
            zstd.stdout.close()
            if upload.wait() != 0 or any(process.wait() != 0 for process in producers):
                raise RuntimeError("Checkpoint stream failed")
            checksum = digest.hexdigest()
            self.verify_remote_size(archive, archive_size)
            manifest = {
                **metadata,
                "run_id": self.run_id,
                "archive_uri": archive,
                "archive_sha256": checksum,
                "archive_size": archive_size,
                "members": members,
                "upload_validation": "size-only",
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            self.publish_json(generation + "/manifest.json", manifest)
            self.publish_json(prefix.rstrip("/") + "/latest.json", {"prefix": generation, **manifest})
            return generation + "/manifest.json"
        finally:
            for process in [*producers, *([upload] if upload else [])]:
                if process.poll() is None:
                    process.terminate()
                    process.wait()

    def restore_checkpoint(self, manifest_uri, destination):
        """Explicit recovery into a new directory; never overwrite a working copy."""
        manifest = json.loads(self.s3("cat", manifest_uri, stdout=subprocess.PIPE, text=True, timeout=60).stdout)
        archive, expected = manifest["archive_uri"], manifest["archive_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or not archive.startswith(manifest_uri.rsplit("/", 1)[0] + "/"):
            raise ValueError("Invalid checkpoint reference")
        members = manifest["members"]
        if (
            not isinstance(members, list)
            or not members
            or any(not isinstance(name, str) or name.startswith("/") or ".." in Path(name).parts for name in members)
        ):
            raise ValueError("Invalid checkpoint member roots")
        if destination.exists():
            raise ValueError("Restore destination must not exist")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=destination.name + ".restoring-", dir=destination.parent))
        (staging / ".restoring").touch()
        digest = hashlib.sha256()
        errors = []
        source = subprocess.Popen([*self.s5, "cat", archive], env=self.env, stdout=subprocess.PIPE)
        decompress = subprocess.Popen(["zstd", "-d"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        def pump():
            try:
                for chunk in iter(lambda: source.stdout.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
                    decompress.stdin.write(chunk)
            except Exception as error:
                errors.append(error)
            finally:
                decompress.stdin.close()

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            with tarfile.open(fileobj=decompress.stdout, mode="r|") as stream:
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
                        with stream.extractfile(member) as handle, output.open("wb") as target:
                            shutil.copyfileobj(handle, target)
                    else:
                        raise ValueError("Checkpoint contains a link or special file")
            # Drain trailing tar padding, allowing the decompressor and pump to finish.
            while decompress.stdout.read(8 * 1024 * 1024):
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
            for process in (source, decompress):
                if process.poll() is None:
                    process.terminate()
                    process.wait()
            thread.join(timeout=5)

    def publish_dumplings(self, height):
        scanner = self.dumps / "Scanner"
        scanner_complete(scanner, height)
        prefix = self.c["dumplings_uri"].rstrip("/") + "/generations/" + self.run_id
        files = {}
        paths = sorted(path for path in scanner.iterdir() if path.is_file())
        for i, path in enumerate(paths):
            files[path.name] = self.upload_verified(path, prefix + "/" + path.name)
            self.progress("dumplings-backup", i + 1, len(paths))
        latest = {"run_id": self.run_id, "prefix": prefix, "last_processed_height": height, "files": files}
        self.publish_json(prefix + "/manifest.json", latest)
        self.publish_json(self.c["dumplings_uri"].rstrip("/") + "/latest.json", latest)
        return prefix + "/manifest.json"

    def synchronize_core(self):
        target = self.c.get("target_height")
        last_report = None
        while True:
            try:
                info = self.rpc("getblockchaininfo")
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
                        and (self.chain == "regtest" or self.rpc("getnetworkinfo")["connections"] > 0)
                    ):
                        break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                self.progress("core-rpc-wait")
            if self.core.poll() is not None:
                raise RuntimeError("Core stopped during synchronization")
            # Bounded tests must not keep downloading for a full reporting interval.
            time.sleep(min(self.c["poll_seconds"], 1) if target is not None else self.c["poll_seconds"])
        self.rpc("setnetworkactive", "false")
        # Disabling P2P does not drain blocks already being validated. A clean
        # offline restart makes the height stable before Dumplings pins it.
        self.stop_core()
        self.start_core(True)
        frozen = self.rpc("getblockchaininfo")
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

    def producer(self):
        self.stage = "producer"
        self.acquire_dataset()
        atomic_json(self.root / "current-ready.json", {"run_id": self.run_id, "state": "preparing"})
        if (self.run / "ready.json").exists():
            raise ValueError("A published generation cannot be reused; choose a fresh run ID")
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
            anchor = json.loads(anchor_path.read_text())
            anchor_height = anchor.get("height")
            if (
                not isinstance(anchor_height, int)
                or isinstance(anchor_height, bool)
                or not start - 1 <= anchor_height <= height
            ):
                raise ValueError("Dumplings chain anchor does not cover its saved progress")
            if self.rpc("getblockhash", str(anchor_height)) != anchor.get("hash"):
                raise ValueError("Dumplings checkpoint is on a different fork; explicit recovery required")
        # Bind even a partially completed scan to this frozen chain. A retry can
        # resume C# checkpoints, but never silently carry detections across a reorg.
        scanner.mkdir(parents=True, exist_ok=True)
        atomic_json(anchor_path, {"height": height, "hash": block_hash})

        def scan_progress():
            try:
                current = int((self.dumps / "Scanner/LastProcessedBlockHeight.txt").read_text())
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
        scanner_complete(self.dumps / "Scanner", height)
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
        self.publish_json(self.remote + "/synchronization/ready.json", ready)
        self.progress("ready-for-blocksci", state="done", height=height)
        self.publish_json(self.remote + "/.pbs/mainnet-producer.done", ready)
        self.release_dataset()

    def parse_progress(self):
        path = self.run / "blocksci.log"
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

    def blocksci(self):
        self.stage = "blocksci"
        self.acquire_dataset()
        ready = json.loads((self.run / "ready.json").read_text())
        height = require_ready(ready, self.run_id)
        if ready.get("network", self.network) != self.network:
            raise ValueError("The ready generation uses a different network")
        current = json.loads((self.root / "current-ready.json").read_text())
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
            if (self.index / "files.txt").read_text() != index_inventory(self.index):
                raise ValueError("Persistent index does not match its file inventory; restore it before updating")
            previous = json.loads((self.index / "manifest.json").read_text())
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
            value = json.loads(config.read_text())
            value["parser"]["maxBlockNum"] = height + 1
            atomic_json(config, value)
        resume = self.index / "parsed/parser/blockList.dat.resume.json"
        resume.unlink(missing_ok=True)
        self.run_command(
            self.container_command(image, "blocksci_parser", config, "update", readonly=True, index_writable=True),
            "blocksci",
            self.parse_progress,
        )
        number = resume_file(self.index, self.core_dir / "blocks")
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
        verified = json.loads(output.read_text())
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
            "published_at": datetime.now(timezone.utc).isoformat(),
            **verified,
        }
        atomic_json(self.index / "manifest.json", manifest)
        (self.index / "files.txt").write_text(index_inventory(self.index))
        # Writer lock remains held. Readers may use the index only after all steps exit.
        self.progress("index-backup")
        index_backup = self.backup(
            self.index, ["config.json", "files.txt", "manifest.json", "parsed"], self.c["index_backup_uri"], manifest
        )
        marker.unlink()
        self.publish_json(self.remote + "/blocksci-index-manifest.json", manifest)
        self.progress("pruning")
        info = self.start_core(True)
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
                    "dumplings/Scanner",
                    "archive-state",
                ],
                self.c["backup_uri"],
                {"height": height, "hash": ready["hash"], "index_backup": index_backup, **signature},
            )
        done = {**ready, "state": "done", "core_checkpoint": checkpoint, "index_backup": index_backup}
        self.progress("complete", state="done", height=height)
        self.publish_json(self.remote + "/.pbs/mainnet-blocksci.done", done)
        self.publish_json(self.remote + "/synchronization/done.json", done)
        self.release_dataset()
        atomic_json(self.run / "done.json", done)

    def cleanup(self):
        for process in self.children:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=30)
        if self.core is not None:
            # Cleanup needs proof of process termination; checkpoint callers
            # additionally require a successful exit before publishing data.
            self.stop_core(require_clean_exit=False)

    def publish_logs(self):
        for path in sorted(self.run.glob("*.log")):
            self.upload(path, self.remote + "/logs/" + path.name)


def main(argv=None):
    args = argv or sys.argv[1:]
    settings = Path(args[0])
    worker = Worker(json.loads(settings.read_text(encoding="utf-8")), settings.parent)
    if args[1] == "restore":
        worker.restore_checkpoint(args[2], Path(args[3]))
        return
    if args[1] == "failed":
        worker.stage = args[2].removeprefix("mainnet-")
        if (len(args) > 3 and args[3] == "0") or (worker.run / f"{worker.stage}.quiescent").is_file():
            worker.release_dataset()
        worker.progress("failed", state="failed", error="PBS stage failed; inspect the persistent job logs")
        worker.publish_json(worker.remote + f"/.pbs/mainnet-{worker.stage}.failed", {"run_id": worker.run_id})
        return
    worker.stage = args[1]

    def interrupted(_signum, _frame):
        raise InterruptedError("PBS job interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        getattr(worker, args[1])()
    finally:
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
