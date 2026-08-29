"""Input validation for values interpolated into PBS job scripts."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path


class PBSError(RuntimeError):
    """Raised when PBS submission or execution fails."""


def walltime_to_seconds(walltime: str) -> int:
    """Convert PBS walltime (HH:MM:SS or DD:HH:MM:SS) to seconds."""
    if not isinstance(walltime, str) or not re.fullmatch(r"[0-9]+(?::[0-9]+){2,3}", walltime):
        raise PBSError(f"Unsupported PBS walltime format: {walltime}")
    parts = walltime.split(":")
    if len(parts) == 3:
        days = "0"
        hours, minutes, seconds = parts
    elif len(parts) == 4:
        days, hours, minutes, seconds = parts
    else:
        raise PBSError(f"Unsupported PBS walltime format: {walltime}")
    day_value, hour_value = int(days), int(hours)
    minute_value, second_value = int(minutes), int(seconds)
    if minute_value >= 60 or second_value >= 60 or (len(parts) == 4 and hour_value >= 24):
        raise PBSError(f"Unsupported PBS walltime format: {walltime}")
    total = (((day_value * 24) + hour_value) * 60 + minute_value) * 60 + second_value
    if total <= 0:
        raise PBSError("PBS walltime must be greater than zero")
    return total


def require_qsub() -> None:
    """Ensure ``qsub`` is available; this must run on a MetaCentrum frontend."""
    if shutil.which("qsub") is None:
        raise PBSError("PBS stages must be run on a MetaCentrum frontend with qsub available")


# Paths are rendered into PBS shell templates via str.format; restrict them to
# characters that survive both PBS directives and unquoted shell contexts.
SAFE_TEMPLATE_PATH_RE = re.compile(r"^[A-Za-z0-9/._+:@-]+$")
SAFE_PBS_SIZE_RE = re.compile(r"^[1-9][0-9]*(?:b|kb|mb|gb|tb)$", re.IGNORECASE)
SAFE_IMAGE_RE = re.compile(r"^[A-Za-z0-9/._+:@%=-]+$")
SAFE_PBS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def require_safe_template_path(path: Path, description: str) -> None:
    if not SAFE_TEMPLATE_PATH_RE.fullmatch(str(path)):
        raise PBSError(f"{description} contains characters unsafe for PBS job templates: {path}")


def require_safe_image(image: str, description: str = "container image") -> None:
    """Reject shell metacharacters before interpolating an image into a PBS script."""
    if not isinstance(image, str) or not image or not SAFE_IMAGE_RE.fullmatch(image):
        raise PBSError(f"{description} contains characters unsafe for PBS job templates: {image}")


def require_safe_pbs_resources(ncpus: int, mem: str, scratch: str, walltime: str) -> None:
    """Validate values interpolated into ``#PBS -l`` directives."""
    if isinstance(ncpus, bool) or not isinstance(ncpus, int) or ncpus <= 0:
        raise PBSError("PBS ncpus must be a positive integer")
    if not isinstance(mem, str) or not SAFE_PBS_SIZE_RE.fullmatch(mem):
        raise PBSError(f"Unsupported PBS memory value: {mem}")
    if not isinstance(scratch, str) or not SAFE_PBS_SIZE_RE.fullmatch(scratch):
        raise PBSError(f"Unsupported PBS scratch value: {scratch}")
    walltime_to_seconds(walltime)


def require_safe_pbs_token(value: str, description: str) -> None:
    if not isinstance(value, str) or not value or not SAFE_PBS_TOKEN_RE.fullmatch(value):
        raise PBSError(f"{description} contains characters unsafe for PBS job templates: {value}")


def require_storage_path(run_dir: Path) -> None:
    """PBS jobs need the run directory on shared MetaCentrum storage (/storage)."""
    resolved = str(run_dir.resolve())
    if not resolved.startswith("/storage/"):
        raise PBSError(f"PBS jobs need run-dir on shared MetaCentrum storage (/storage), not: {resolved}")
    require_safe_template_path(run_dir.resolve(), "PBS path")


def require_existing_path(path: Path, description: str) -> None:
    """Ensure a path used by the PBS job exists before submitting."""
    if not path.exists():
        raise PBSError(f"{description} does not exist: {path}")


def require_bitcoin_datadir(path: Path) -> None:
    """Ensure the supplied Bitcoin Core datadir has the shape BlockSci expects."""
    require_existing_path(path, "PBS Bitcoin datadir")
    regtest_dir = path / "regtest"
    if regtest_dir.is_dir() and not os.access(regtest_dir, os.R_OK | os.X_OK):
        # bitcoind creates regtest/ as 0700, so a node that ran under a
        # different uid leaves the datadir present but unreadable here - and
        # unreadable for the BlockSci job too.
        raise PBSError(
            f"PBS Bitcoin datadir is not readable by the current user (uid {os.getuid()}): "
            f"{regtest_dir} is owned by uid {regtest_dir.stat().st_uid} with mode "
            f"{regtest_dir.stat().st_mode & 0o777:o}. The btc-node must run as the storage "
            "identity (KUBERNETES_STORAGE_UID/KUBERNETES_STORAGE_GID)."
        )
    if not (regtest_dir / "blocks").is_dir():
        raise PBSError(f"PBS Bitcoin datadir must contain regtest/blocks so BlockSci can read it: {path}")
