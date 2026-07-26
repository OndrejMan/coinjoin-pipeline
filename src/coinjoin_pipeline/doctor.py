"""Non-mutating host preflight checks."""

from __future__ import annotations

from enum import Enum, auto
import os
from importlib.resources import files
from pathlib import Path
import shutil
import subprocess

from .images import Images
from .commands import DOCKERLESS_RESEARCH_ACTIONS, has_option, option_value


class Capability(Enum):
    """Host tools an action actually needs, derived from the action itself."""

    CONTAINER_RUNTIME = auto()
    KUBECTL = auto()
    QSUB = auto()
    S5CMD_FRONTEND = auto()


PBS_FLAGS = ("--analysisPbs", "--blocksciPbs", "--mappingsPbs")


def required_capabilities(action: str, arguments: list[str]) -> set[Capability]:
    """Map an action to the host tools it needs.

    Replaces the previous frontend env-var switch: a pure S3
    full-run on a PBS frontend needs qsub, kubectl and s5cmd but never Docker,
    while a local run needs the container runtime and nothing else.
    """
    capabilities: set[Capability] = set()
    driver = option_value(arguments, "--driver")
    backend = option_value(arguments, "--artifact-backend")
    uses_s3 = backend == "s3"
    uses_pbs = any(has_option(arguments, flag) for flag in PBS_FLAGS) or action == "pbs-from-s3"
    # Dry runs never reach the cluster or the queue, matching the previous
    # `not dry_run` guards on the kubernetes and qsub checks.
    live = not has_option(arguments, "--dry-run")

    if driver == "kubernetes" and live:
        capabilities.add(Capability.KUBECTL)
    # `run_full_run_s3` calls `require_qsub()` unconditionally after the dry-run
    # branch, so an S3 full-run needs the queue even without a --*Pbs flag;
    # without this the preflight would pass and the wrapper would fail later.
    if (uses_pbs or (uses_s3 and action == "full-run")) and live:
        capabilities.add(Capability.QSUB)
    if (uses_s3 or action == "pbs-from-s3") and live:
        capabilities.add(Capability.S5CMD_FRONTEND)
    # The frontend needs a local daemon only when it still runs containers
    # itself: S3 hands emulation to a Kubernetes Job, and a PBS-only stage runs
    # under Singularity on the compute node. full-run/emulate keep emulating
    # locally even with PBS analysis flags, so they stay on the runtime.
    # The read-only `runs`/`scenarios` actions only inspect the runs tree
    # in-process, so demanding Docker would make them unusable on a Docker-less
    # PBS frontend — which is exactly where the S3 workflow lives. `runs
    # validate` is not one of them; it runs the BlockSci image.
    delegated = (
        uses_s3
        or (uses_pbs and action not in {"full-run", "emulate"})
        or action in DOCKERLESS_RESEARCH_ACTIONS
    )
    if not delegated:
        capabilities.add(Capability.CONTAINER_RUNTIME)
    return capabilities


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
    pbs_datadir = option_value(arguments, "--pbs-bitcoin-datadir") or os.environ.get(
        "PBS_BITCOIN_DATADIR"
    )
    if has_option(arguments, "--blocksciPbs") and pbs_datadir:
        # Kubernetes emulation without --copy-to-host fills this directory
        # itself; every other path must already hold a parsed chain.
        action_words = [item for item in arguments if not item.startswith("-")]
        kubernetes_fills_it = (
            option_value(arguments, "--driver") == "kubernetes"
            and any(word in {"full-run", "emulate"} for word in action_words)
            and not has_option(arguments, "--copy-to-host")
        )
        if not kubernetes_fills_it and not (Path(pbs_datadir).expanduser() / "regtest/blocks").is_dir():
            errors.append(
                f"PBS Bitcoin datadir must contain regtest/blocks: {pbs_datadir}"
            )
    run_dir = option_value(arguments, "--run-dir")
    if run_dir:
        selected = Path(run_dir).expanduser()
        if not selected.is_absolute():
            selected = runs_root / selected
        if not selected.is_dir():
            errors.append(f"run directory not found: {selected}")
    return errors


# Entries the wrapper itself opens for writing on every mutating run. A runs
# root last used through the wrapper *image* owns them as root, because that
# wrapper ran as root inside its container; the bare wrapper runs as the
# invoking user and cannot reopen them.
CONTAINER_ERA_ENTRIES = (".pipeline.lock", ".notebooks")


def inherited_root_ownership_errors(runs_root: Path) -> list[str]:
    """Report pre-existing runs-root entries this user can no longer write."""
    errors: list[str] = []
    for name in CONTAINER_ERA_ENTRIES:
        entry = runs_root / name
        if entry.exists() and not os.access(entry, os.W_OK):
            errors.append(
                f"{entry} is not writable by the current user; it was created by the "
                "wrapper container running as root. Take the runs root over with "
                f"`sudo chown -R $(id -u):$(id -g) {runs_root}` or use --runs-root"
            )
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
    # `None` keeps the historical behaviour (always probe the runtime) for
    # callers such as `cjp doctor` that have no action to derive from.
    wants_runtime = capabilities is None or Capability.CONTAINER_RUNTIME in capabilities
    executable = shutil.which(runtime)
    if wants_runtime:
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
    for capability, command, purpose in (
        (Capability.KUBECTL, "kubectl", "the Kubernetes driver"),
        (Capability.QSUB, "qsub", "PBS execution"),
        (Capability.S5CMD_FRONTEND, "s5cmd", "S3 artifact transport"),
    ):
        if capabilities and capability in capabilities and shutil.which(command) is None:
            errors.append(f"{command} command not found for {purpose}")
    probe = runs_root if runs_root.exists() else runs_root.parent
    if not probe.exists() or not os.access(probe, os.W_OK):
        errors.append(f"output directory is not writable: {runs_root}")
    else:
        errors.extend(inherited_root_ownership_errors(runs_root))
    if executable and check_images and wants_runtime:
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
