"""Input validation for values interpolated into PBS job scripts."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .defaults import PBS_MULTIJOB_LOCATIONS, PBS_SCRATCH_TYPES, PBSResources


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


def require_safe_pbs_resources(resources: PBSResources, multijob: str | None = None) -> None:
    """Validate values interpolated into ``#PBS -l`` directives."""
    ncpus = resources.ncpus
    if isinstance(ncpus, bool) or not isinstance(ncpus, int) or ncpus <= 0:
        raise PBSError("PBS ncpus must be a positive integer")
    for value, name in ((resources.mem, "memory"), (resources.scratch, "scratch")):
        if not isinstance(value, str) or not SAFE_PBS_SIZE_RE.fullmatch(value):
            raise PBSError(f"Unsupported PBS {name} value: {value}")
    if resources.scratch_type not in PBS_SCRATCH_TYPES:
        raise PBSError(f"Unsupported PBS scratch type: {resources.scratch_type}")
    if multijob is not None and multijob not in PBS_MULTIJOB_LOCATIONS:
        raise PBSError(f"Unsupported PBS multijob location: {multijob}")
    walltime_to_seconds(resources.walltime)


def require_notebook_port(port: int) -> int:
    """The Jupyter port of a notebook job: an unprivileged TCP port."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise PBSError("BlockSci notebook port must be between 1024 and 65535")
    return port


def require_safe_pbs_token(value: str, description: str) -> None:
    if not isinstance(value, str) or not value or not SAFE_PBS_TOKEN_RE.fullmatch(value):
        raise PBSError(f"{description} contains characters unsafe for PBS job templates: {value}")


# Frontends such as skirit link /storage/<site>/home to the /auto automounter, which
# every compute node mounts too, so a resolved shared path may start with either.
SHARED_STORAGE_ROOTS = ("/storage/", "/auto/")


def multijob_location(path: str) -> str | None:
    """The MetaCentrum multijob scratch a path is on (``/scratch/multijob-<location>/``), else None."""
    return next((place for place in PBS_MULTIJOB_LOCATIONS if path.startswith(f"/scratch/multijob-{place}/")), None)


def kept_between_jobs(path: str) -> bool:
    """Whether later jobs find the path again: shared storage or a multijob scratch, without '..'."""
    on_shared = path.startswith(SHARED_STORAGE_ROOTS) or multijob_location(path) is not None
    return on_shared and ".." not in Path(path).parts


def require_storage_path(run_dir: Path) -> None:
    """PBS jobs need the path on shared MetaCentrum storage (/storage or its /auto mount)."""
    resolved = str(run_dir.resolve())
    if not resolved.startswith(SHARED_STORAGE_ROOTS):
        raise PBSError(f"PBS jobs need run-dir on shared MetaCentrum storage (/storage), not: {resolved}")
    require_safe_template_path(run_dir.resolve(), "PBS path")


def require_existing_path(path: Path, description: str) -> None:
    """Ensure a path used by the PBS job exists before submitting."""
    if not path.exists():
        raise PBSError(f"{description} does not exist: {path}")
