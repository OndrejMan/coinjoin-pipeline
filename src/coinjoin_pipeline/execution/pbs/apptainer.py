"""Apptainer (Singularity) commands on a PBS node, shared by the S3 stages and the mainnet jobs.

Standard library only and Python 3.8 syntax, like every module a PBS job ships.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

MISSING_RUNTIME = "Apptainer or Singularity is required on the PBS node"


def find_runtime() -> str | None:
    """The node's ``singularity``, or ``apptainer`` where only that name is installed."""
    return shutil.which("singularity") or shutil.which("apptainer")


def exec_command(
    runtime: str,
    image: str,
    command: Iterable[object],
    *,
    binds: Sequence[str] = (),
    environment: Sequence[str] = (),
    cleanenv: bool = False,
    pwd: str | Path | None = None,
) -> list[str]:
    """``<runtime> exec`` of command in image, with these binds and ``NAME=value`` variables."""
    arguments = [runtime, "exec", *(["--cleanenv"] if cleanenv else [])]
    for bind in binds:
        arguments += ["--bind", bind]
    if pwd is not None:
        arguments += ["--pwd", str(pwd)]
    for variable in environment:
        arguments += ["--env", variable]
    return [*arguments, image, *map(str, command)]
