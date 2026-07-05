"""Non-mutating host preflight checks."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
import shutil
import subprocess

from .images import Images
from .commands import has_option, option_value


def validate_arguments(arguments: list[str], runs_root: Path) -> list[str]:
    """Validate host-visible files and tools selected by pipeline arguments."""
    errors: list[str] = []
    scenario = option_value(arguments, "--scenario")
    if scenario:
        candidate = Path(scenario).expanduser()
        packaged = files("coinjoin_pipeline").joinpath(f"resources/scenarios/{candidate.name}")
        if not candidate.is_file() and not (Path.cwd() / candidate).is_file() and not packaged.is_file():
            errors.append(f"scenario not found: {scenario}")
    dry_run = has_option(arguments, "--dry-run")
    if option_value(arguments, "--driver") == "kubernetes" and not dry_run:
        kubeconfig = Path(option_value(arguments, "--kubeconfig") or Path.home() / ".kube/config").expanduser()
        if not kubeconfig.is_file():
            errors.append(f"kubeconfig not found: {kubeconfig}")
        if shutil.which("kubectl") is None:
            errors.append("kubectl command not found for Kubernetes driver")
    uses_pbs = any(has_option(arguments, flag) for flag in ("--analysisPbs", "--blocksciPbs", "--mappingsPbs"))
    if uses_pbs and os.environ.get("PBS_FRONTEND_DIRECT") == "1" and not dry_run:
        if shutil.which("qsub") is None:
            errors.append("qsub command not found for direct PBS execution")
    run_dir = option_value(arguments, "--run-dir")
    if run_dir:
        selected = Path(run_dir).expanduser()
        if not selected.is_absolute():
            selected = runs_root / selected
        if not selected.is_dir():
            errors.append(f"run directory not found: {selected}")
    return errors


def check(
    runtime: str,
    runs_root: Path,
    images: Images,
    *,
    check_images: bool = True,
    image_components: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if runtime not in {"docker", "podman"}:
        return [f"unsupported runtime {runtime!r}; expected docker or podman"]
    executable = shutil.which(runtime)
    if not executable:
        errors.append(f"{runtime} command not found")
    else:
        try:
            result = subprocess.run(
                [executable, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=10,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{runtime} daemon/API check timed out")
            result = None
        if result is not None and result.returncode:
            errors.append(f"{runtime} daemon/API is not reachable")
    probe = runs_root if runs_root.exists() else runs_root.parent
    if not probe.exists() or not os.access(probe, os.W_OK):
        errors.append(f"output directory is not writable: {runs_root}")
    if executable and check_images:
        selected = set(images.as_dict()) if image_components is None else image_components
        for component, image in images.as_dict().items():
            if component not in selected:
                continue
            try:
                local = subprocess.run(
                    [executable, "image", "inspect", image], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, check=False, timeout=10,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"image inspection timed out: {image}")
                continue
            if local.returncode == 0:
                continue
            reference = f"docker://{image}" if runtime == "podman" else image
            try:
                remote = subprocess.run(
                    [executable, "manifest", "inspect", reference], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, check=False, timeout=20,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"registry image check timed out: {image}")
                continue
            if remote.returncode:
                errors.append(f"image is unavailable locally and from its registry: {image}")
    return errors
