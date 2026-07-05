"""Thin `lint` command that forwards to `ruff check`."""

from __future__ import annotations

import shutil
import subprocess
import sys


def main() -> int:
    targets = sys.argv[1:] or ["."]
    ruff = shutil.which("ruff")
    if ruff is None:
        print(
            "ruff is not installed; run `uv sync` to install the dev group",
            file=sys.stderr,
        )
        return 127
    return subprocess.call([ruff, "check", *targets])
