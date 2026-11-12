"""Apptainer (Singularity) commands on a PBS node, shared by the S3 stages and the mainnet jobs.

Standard library only and Python 3.8 syntax, like every module a PBS job ships.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

MISSING_RUNTIME = "Apptainer or Singularity is required on the PBS node"


def find_runtime() -> str | None:
    """The node's ``singularity``, or ``apptainer`` where only that name is installed."""
    return shutil.which("singularity") or shutil.which("apptainer")


def alias_binds(path: str | Path, mode: str = "rw") -> list[str]:
    """Bind path at itself, and at its target too when path is an alias.

    MetaCentrum's /scratch/multijob-* aliases point outside /scratch, which the node binds
    into every container: the alias alone resolves to nothing there and the bind is lost.
    """
    alias = str(Path(path))
    real = os.path.realpath(alias)
    binds = [f"{real}:{real}:{mode}"]
    if real != alias:
        binds.append(f"{real}:{alias}:{mode}")
    return binds


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
