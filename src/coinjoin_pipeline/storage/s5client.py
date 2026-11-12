"""s5cmd access shared by the frontend and the self-contained PBS workers.

Standard library only and Python 3.8 syntax: a PBS job copies this file next to its
worker script and runs it with the node's ``python3``.

It also owns the format of a single backup (the persistent index, the Core checkpoint):
one archive, its ``.sha256`` and ``manifest.json`` under a prefix. ``replace_backup``
writes it and ``restore_backup`` reads it, so the two cannot drift apart.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import IO, Iterator, Mapping, Sequence

# Read size of the archive streams; large enough to keep s5cmd and zstd busy.
CHUNK = 8 * 1024 * 1024
# s5cmd cannot size the parts of a stream and S3 allows 10,000 of them: its default of
# 50 MiB ends at 488 GiB, within reach of the archived index of the whole chain.
PIPE_PART_SIZE_MIB = 256
SHA256 = re.compile(r"[0-9a-f]{64}")


class S5Error(RuntimeError):
    """An s5cmd call failed for a reason other than a missing object."""


def no_object_found(result: subprocess.CompletedProcess) -> bool:
    """s5cmd's answer to a listing that matched nothing; its exit status is nonzero."""
    return "no object found" in ((result.stderr or "") + (result.stdout or "")).lower()


def pipe(stream: IO[bytes] | None) -> IO[bytes]:
    """The pipe a process was started with; every caller asked for one."""
    if stream is None:
        raise RuntimeError("The process has no pipe to stream through")
    return stream


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


def new_staging(destination: Path) -> Path:
    """An empty directory next to destination, which must not exist yet."""
    if destination.exists():
        raise ValueError("Restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=destination.name + ".restoring-", dir=destination.parent))


@contextlib.contextmanager
def _stage(
    command: Sequence[str],
    stdin: int | IO[bytes] | None = None,
    stdout: int | None = None,
    env: Mapping[str, str] | None = None,
) -> Iterator[subprocess.Popen]:
    """One process of a stream; leaving the context terminates it if needed and reaps it."""
    with subprocess.Popen(command, stdin=stdin, stdout=stdout, env=env) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()


class S5Client:
    """One endpoint, credentials file and profile; every call sees no AWS_* variable.

    Credentials therefore come only from the configured file and profile, never from
    whatever the job or frontend environment happens to export.
    """

    def __init__(self, endpoint_url: str, credentials_file: str, profile: str) -> None:
        self.base = [
            "s5cmd",
            "--credentials-file",
            credentials_file,
            "--profile",
            profile,
            "--endpoint-url",
            endpoint_url,
        ]
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}

    def command(self, *arguments: object) -> list[str]:
        return [*self.base, *map(str, arguments)]

    def run(self, *arguments: object, check: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess:
        """Run one command, capturing its text output."""
        return subprocess.run(
            self.command(*arguments),
            env=self.environment,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

    def listing(self, pattern: str, timeout: float | None = 600) -> list[dict[str, object]]:
        """The JSON entries ``ls`` prints for pattern; empty when nothing matches.

        ``ls`` lists by prefix: ``blk.dat`` also lists ``blk.dat.json``.
        """
        result = self.run("--json", "ls", pattern, timeout=timeout)
        if result.returncode != 0:
            if no_object_found(result):
                return []
            raise S5Error(f"s5cmd ls {pattern} failed (exit {result.returncode}): {(result.stderr or '').strip()}")
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def exists(self, uri: str, timeout: float | None = 600) -> bool:
        """Whether the exact key exists, not merely a key that starts with it."""
        return any(entry.get("key") == uri for entry in self.listing(uri, timeout))

    def object_size(self, uri: str, timeout: float | None = 600) -> object | None:
        """The listed size of the exact key, None when it is missing.

        s5cmd leaves out the size of an empty object, which therefore reads as 0.
        The value is returned as listed, so callers can reject a non-integer.
        """
        for entry in self.listing(uri, timeout):
            if entry.get("key") == uri:
                return entry.get("size", 0)
        return None

    def sizes(self, prefix: str, timeout: float | None = 600) -> dict[str, object]:
        """Relative key -> size of every object below prefix, which ends with a slash."""
        return {
            str(entry["key"])[len(prefix) :]: entry.get("size", 0)
            for entry in self.listing(prefix + "*", timeout)
            if entry.get("type", "file") == "file" and str(entry.get("key", "")).startswith(prefix)
        }

    def cat(self, uri: str, timeout: float | None = 60) -> str:
        return self.run("cat", uri, check=True, timeout=timeout).stdout

    def cat_json(self, uri: str, timeout: float | None = 60) -> dict:
        """A JSON object stored at uri."""
        value = json.loads(self.cat(uri, timeout))
        if not isinstance(value, dict):
            raise S5Error(f"{uri} does not hold a JSON object")
        return value

    def copy(self, source: object, destination: str, timeout: float | None = 3600) -> None:
        self.run("cp", source, destination, check=True, timeout=timeout)

    def remove(self, *uris: str, timeout: float | None = 600) -> bool:
        """Best-effort delete; a missing object is not an error. True when s5cmd succeeded."""
        return self.run("rm", *uris, timeout=timeout).returncode == 0

    def upload_archive(self, root: Path, members: Sequence[str], uri: str, threads: int) -> tuple[str, int]:
        """Stream ``tar -C root members | zstd`` to uri; the archive's SHA-256 and size.

        Hashed and measured on the way, so nothing is read back. s5cmd needs ``pipe``
        (2.2 or later); every node and image the pipeline runs on has 2.3.
        """
        digest, size = hashlib.sha256(), 0
        with contextlib.ExitStack() as stack:
            tar = stack.enter_context(
                _stage(["tar", "--format=pax", "-C", str(root), "-cf", "-", *members], stdout=subprocess.PIPE)
            )
            zstd = stack.enter_context(_stage(["zstd", "-3", f"-T{threads}"], stdin=tar.stdout, stdout=subprocess.PIPE))
            pipe(tar.stdout).close()
            command = self.command("pipe", "--part-size", PIPE_PART_SIZE_MIB, uri)
            upload = stack.enter_context(
                _stage(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env=self.environment)
            )
            processes = [tar, zstd, upload]
            destination = pipe(upload.stdin)
            compressed = pipe(zstd.stdout)
            for chunk in iter(lambda: compressed.read(CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
                destination.write(chunk)
            destination.close()
            compressed.close()
            if any(process.wait() != 0 for process in processes):
                raise S5Error(f"Streaming {root} to {uri} failed")
            return digest.hexdigest(), size

    @contextlib.contextmanager
    def unpacked(self, uri: str, expected_sha256: str | None = None) -> Iterator[IO[bytes]]:
        """The content of the zstd object ``uri`` as a stream.

        The body reads the stream, or closes it after handing it to a process. Leaving the
        context waits for the download and checks it, with expected_sha256 also the object's
        hash, so the body's result counts only once the context ended without S5Error.
        """
        digest = hashlib.sha256()
        failures: list[Exception] = []
        with contextlib.ExitStack() as stack:
            source = stack.enter_context(_stage(self.command("cat", uri), stdout=subprocess.PIPE, env=self.environment))
            unpack = stack.enter_context(_stage(["zstd", "-q", "-d"], stdin=subprocess.PIPE, stdout=subprocess.PIPE))
            downloaded, compressed, stream = pipe(source.stdout), pipe(unpack.stdin), pipe(unpack.stdout)

            def pump() -> None:
                try:
                    for chunk in iter(lambda: downloaded.read(CHUNK), b""):
                        digest.update(chunk)
                        compressed.write(chunk)
                except (OSError, ValueError) as error:
                    failures.append(error)
                finally:
                    # A dead zstd fails the flush; its exit status reports that.
                    with contextlib.suppress(OSError, ValueError):
                        compressed.close()

            # A thread, because the body reads the other end of this pipeline.
            thread = threading.Thread(target=pump, daemon=True)
            thread.start()
            yield stream
            if not stream.closed:
                # Trailing tar padding: zstd and the download end only once it was read.
                while stream.read(CHUNK):
                    pass
            thread.join(timeout=30)
            if thread.is_alive() or failures or source.wait() != 0 or unpack.wait() != 0:
                raise S5Error(f"Reading {uri} failed")
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise S5Error("The S3 archive does not match its SHA-256")

    def restore_archive(self, uri: str, destination: Path, expected_sha256: str) -> None:
        """Stream ``uri`` through ``zstd -d | tar -x -C destination``, hashing it on the way.

        The caller exposes what was extracted only after this returns.
        """
        with self.unpacked(uri, expected_sha256) as stream:
            with _stage(["tar", "-C", str(destination), "-xf", "-"], stdin=stream) as extract:
                stream.close()
                if extract.wait() != 0:
                    raise S5Error(f"Restoring {uri} failed")

    def copy_text(self, content: str, uri: str) -> None:
        """Upload a small text object."""
        handle, staged = tempfile.mkstemp(prefix="s5-object-")
        try:
            with os.fdopen(handle, "w") as stream:
                stream.write(content)
            self.copy(staged, uri)
        finally:
            Path(staged).unlink(missing_ok=True)

    def require_size(self, uri: str, expected: int) -> None:
        """The object is listed under exactly this key with this size; it is not read back."""
        listed = self.object_size(uri)
        if isinstance(listed, bool) or listed != expected:
            raise S5Error("S3 size/key mismatch: " + uri)

    def replace_backup(
        self, root: Path, members: Sequence[str], prefix: str, name: str, metadata: Mapping[str, object], threads: int
    ) -> dict:
        """Replace the single backup under prefix with ``tar -C root members | zstd``; its manifest.

        S3 never holds two copies: the manifest and the old archive go first, so until the
        new manifest is written there is no valid backup. Only the object size is checked
        after the upload; ``restore_backup`` verifies the SHA-256.
        """
        prefix = prefix.rstrip("/")
        archive, manifest_uri = f"{prefix}/{name}", f"{prefix}/manifest.json"
        for member in members:
            path = root / member
            if not path.exists() or path.is_symlink():
                raise S5Error(f"Missing or linked backup root: {path}")
        member_bytes = tree_bytes(root, members)
        for stale in (manifest_uri, archive, f"{archive}.sha256"):
            self.remove(stale)
        checksum, size = self.upload_archive(root, members, archive, threads)
        self.copy_text(f"{checksum}  {name}\n", f"{archive}.sha256")
        self.require_size(archive, size)
        manifest = {
            **metadata,
            "archive_uri": archive,
            "archive_sha256": checksum,
            "archive_size": size,
            "members": list(members),
            "member_bytes": member_bytes,
            "upload_validation": "size-only",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        self.copy_text(json.dumps(manifest, indent=2) + "\n", manifest_uri)
        return manifest

    def restore_backup(self, manifest_uri: str, destination: Path) -> dict:
        """Restore the backup that manifest_uri describes into destination, which must not exist yet.

        The archive is hashed while it is extracted, and only regular files and directories
        below the manifest's members are accepted. destination appears once all of it was
        verified; a failure leaves nothing behind. Returns the manifest.
        """
        manifest = self.cat_json(manifest_uri)
        archive, expected = manifest.get("archive_uri"), manifest.get("archive_sha256")
        members = manifest.get("members")
        if (
            not isinstance(archive, str)
            or not archive.startswith(manifest_uri.rsplit("/", 1)[0] + "/")
            or not isinstance(expected, str)
            or not SHA256.fullmatch(expected)
        ):
            raise S5Error("Invalid backup reference: " + manifest_uri)
        if (
            not isinstance(members, list)
            or not members
            or any(not isinstance(name, str) or name.startswith("/") or ".." in Path(name).parts for name in members)
        ):
            raise S5Error("Invalid backup member roots: " + manifest_uri)
        roots = [Path(name) for name in members]
        staging = new_staging(destination)
        try:
            with self.unpacked(archive, expected) as archive_stream:
                with tarfile.open(fileobj=archive_stream, mode="r|") as stream:
                    for member in stream:
                        relative = Path(member.name)
                        if (
                            relative.is_absolute()
                            or ".." in relative.parts
                            or not any(relative == root or root in relative.parents for root in roots)
                        ):
                            raise S5Error("The backup contains a path outside its member roots")
                        output = staging / relative
                        if member.isdir():
                            output.mkdir(parents=True, exist_ok=True)
                        elif member.isfile():
                            output.parent.mkdir(parents=True, exist_ok=True)
                            with pipe(stream.extractfile(member)) as handle, output.open("wb") as target:
                                shutil.copyfileobj(handle, target)
                            # Archive markers compare nanosecond mtimes; float tar timestamps lose precision.
                            mtime_ns = int(Decimal(member.pax_headers.get("mtime", str(member.mtime))) * 1_000_000_000)
                            os.utime(output, ns=(mtime_ns, mtime_ns))
                        else:
                            raise S5Error("The backup contains a link or special file")
            if any(not (staging / root).exists() for root in roots):
                raise S5Error("The backup lacks a declared member")
            staging.rename(destination)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return manifest
