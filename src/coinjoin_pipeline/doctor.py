"""Non-mutating host preflight checks."""

from __future__ import annotations

import os
import shutil
import subprocess
from enum import Enum, auto
from pathlib import Path

from .configuration import PipelineConfiguration
from .images import Images
from .runs import scenario_file


class Capability(Enum):
    """Host tools an action actually needs, derived from the action itself."""

    CONTAINER_RUNTIME = auto()


def required_capabilities(config: PipelineConfiguration) -> set[Capability]:
    return {Capability.CONTAINER_RUNTIME}


def required_image_components(config: PipelineConfiguration) -> set[str]:
    return {
        "emulate": {"emulator"},
        "coinjoin-analysis": {"coinjoin_analysis"},
    }.get(config.action, {"emulator", "coinjoin_analysis"})


def validate_arguments(config: PipelineConfiguration, runs_root: Path) -> list[str]:
    """Check host-visible inputs after effective configuration is resolved."""
    errors: list[str] = []
    if config.scenario and scenario_file(config.scenario, config.engine) is None:
        errors.append(
            f"scenario not found: {config.scenario}; pass a scenario JSON path or the name of a catalogue scenario"
        )
    if config.run_dir:
        selected = Path(config.run_dir).expanduser()
        if not selected.is_absolute():
            selected = runs_root / selected
        if selected.resolve().parent != runs_root.resolve():
            errors.append(f"run directory must be directly inside {runs_root}: {selected}")
        if not selected.is_dir():
            errors.append(f"run directory not found: {selected}")
    return errors


def _exit_status(command: list[str], timeout: int) -> int | None:
    """Exit status of a silent probe, or None when it timed out."""
    try:
        return subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=timeout
        ).returncode
    except subprocess.TimeoutExpired:
        return None


def _image_errors(executable: str, images: Images, selected: set[str]) -> list[str]:
    """Each selected image must exist locally or be pullable from its registry."""
    errors: list[str] = []
    for component, image in images.as_dict().items():
        if component not in selected:
            continue
        local = _exit_status([executable, "image", "inspect", image], timeout=10)
        if local is None:
            errors.append(f"image inspection timed out: {image}")
            continue
        if local == 0:
            continue
        remote = _exit_status([executable, "manifest", "inspect", image], timeout=20)
        if remote is None:
            errors.append(f"registry image check timed out: {image}")
        elif remote:
            errors.append(f"image is unavailable locally and from its registry: {image}")
    return errors


def check(
    runtime: str,
    runs_root: Path,
    images: Images,
    *,
    check_images: bool = True,
    image_components: set[str] | None = None,
    capabilities: set[Capability] | None = None,
) -> list[str]:
    errors: list[str] = []
    if runtime not in {"docker", "podman"}:
        return [f"unsupported runtime {runtime!r}; expected docker or podman"]
    # `None` means no action to derive from: always probe the runtime.
    wants_runtime = capabilities is None or Capability.CONTAINER_RUNTIME in capabilities
    executable = shutil.which(runtime)
    if wants_runtime:
        if not executable:
            errors.append(f"{runtime} command not found")
        else:
            status = _exit_status([executable, "info"], timeout=10)
            if status is None:
                errors.append(f"{runtime} daemon/API check timed out")
            elif status:
                errors.append(f"{runtime} daemon/API is not reachable")
    probe = runs_root if runs_root.exists() else runs_root.parent
    if not probe.exists() or not os.access(probe, os.W_OK):
        errors.append(f"output directory is not writable: {runs_root}")
    if executable and check_images and wants_runtime:
        selected = set(images.as_dict()) if image_components is None else image_components
        errors.extend(_image_errors(executable, images, selected))
    return errors
