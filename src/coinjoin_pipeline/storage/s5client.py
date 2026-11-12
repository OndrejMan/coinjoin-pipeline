"""s5cmd access of the frontend."""

from __future__ import annotations

import json
import os
import subprocess


class S5Error(RuntimeError):
    """An s5cmd call failed for a reason other than a missing object."""


def no_object_found(result: subprocess.CompletedProcess) -> bool:
    """s5cmd's answer to a listing that matched nothing; its exit status is nonzero."""
    return "no object found" in ((result.stderr or "") + (result.stdout or "")).lower()


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

    def copy(self, source: object, destination: str, timeout: float | None = 3600) -> None:
        self.run("cp", source, destination, check=True, timeout=timeout)

    def remove(self, *uris: str, timeout: float | None = 600) -> bool:
        """Best-effort delete; a missing object is not an error. True when s5cmd succeeded."""
        return self.run("rm", *uris, timeout=timeout).returncode == 0
