"""s5cmd access shared by the frontend and the self-contained PBS workers.

Standard library only and Python 3.8 syntax: a PBS job copies this file next to its
worker script and runs it with the node's ``python3``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import IO, Iterator, Mapping, Sequence

# Read size of the archive streams; large enough to keep s5cmd and zstd busy.
CHUNK = 8 * 1024 * 1024


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


def stream_sha256(stream: IO[bytes]) -> str:
    """SHA-256 of everything left in stream."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(CHUNK), b""):
        digest.update(chunk)
    return digest.hexdigest()


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

    def supports_pipe(self) -> bool:
        """Whether this s5cmd uploads from stdin (``pipe``), which s5cmd 2.1 lacks."""
        return self.run("pipe", "--help", timeout=60).returncode == 0

    def upload_archive(self, root: Path, members: Sequence[str], uri: str, threads: int) -> tuple[str, int]:
        """Stream ``tar -C root members | zstd`` to uri; the archive's SHA-256 and size.

        Hashed and measured on the way, so nothing is read back. Without ``pipe`` the
        archive is staged as a temporary file first.
        """
        digest, size = hashlib.sha256(), 0
        staged = None
        with contextlib.ExitStack() as stack:
            tar = stack.enter_context(_stage(["tar", "-C", str(root), "-cf", "-", *members], stdout=subprocess.PIPE))
            zstd = stack.enter_context(_stage(["zstd", "-3", f"-T{threads}"], stdin=tar.stdout, stdout=subprocess.PIPE))
            pipe(tar.stdout).close()
            processes = [tar, zstd]
            if self.supports_pipe():
                command = self.command("pipe", uri)
                upload = stack.enter_context(
                    _stage(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env=self.environment)
                )
                processes.append(upload)
                destination = pipe(upload.stdin)
            else:
                # In the job's temporary directory (its scratch), never in the tree being archived.
                handle, staged = tempfile.mkstemp(prefix="archive-", suffix=".tar.zst")
                stack.callback(Path(staged).unlink, missing_ok=True)
                destination = os.fdopen(handle, "wb")
            compressed = pipe(zstd.stdout)
            for chunk in iter(lambda: compressed.read(CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
                destination.write(chunk)
            destination.close()
            compressed.close()
            if any(process.wait() != 0 for process in processes):
                raise S5Error(f"Streaming {root} to {uri} failed")
            if staged is not None:
                self.copy(staged, uri, timeout=None)
            return digest.hexdigest(), size

    def discard(self, uri: str) -> None:
        """Read an object to its end and keep nothing: the speed of S3 alone."""
        with _stage(self.command("cat", uri), stdout=subprocess.DEVNULL, env=self.environment) as source:
            if source.wait() != 0:
                raise S5Error(f"Reading {uri} failed")

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
