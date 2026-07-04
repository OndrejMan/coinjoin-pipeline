"""Run the project's static analysis tools in the uv environment."""

from __future__ import annotations

import subprocess
import sys


def run(command: list[str]) -> None:
    """Run a tool using the Python interpreter selected by uv."""
    subprocess.run([sys.executable, "-m", *command], check=True)


def main() -> None:
    """Apply Ruff fixes, then run type and lint checks."""
    run(["ruff", "check", ".", "--fix"])
    run(["mypy", "."])
    run(["pylint", "."])
