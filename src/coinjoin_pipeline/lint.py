"""`lint` command: Ruff fixes, then the type and lint checks the package must pass."""

from __future__ import annotations

import subprocess
import sys

TARGETS = ("src/coinjoin_pipeline", "pipeline/exporters")


def main() -> int:
    # CI passes --check so a run reports problems instead of rewriting files.
    ruff = ["ruff", "check", "."] if "--check" in sys.argv[1:] else ["ruff", "check", ".", "--fix"]
    for command in (
        ruff,
        ["mypy"],
        ["pylint", *TARGETS],
    ):
        status = subprocess.call([sys.executable, "-m", *command])
        if status:
            return status
    return 0
