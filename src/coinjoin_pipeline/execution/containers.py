"""EXPERIMENTAL: Local container commands and their filesystem inputs.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from exporters.artifact_paths import BASELINE_FILE, COINJOIN_ANALYSIS_DIR, EMULATOR_DIR
from exporters.analysis_artifact import ARTIFACT_NAME
from exporters.artifact_paths import blocksci_analysis_dir

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.locks import (
    close_locks as close_pipeline_locks,
)
from coinjoin_pipeline.execution.pbs.validation import PBSError
from coinjoin_pipeline.execution.pbs_settings import unified_report_image_reference
from coinjoin_pipeline.execution.pipeline_logging import (
    captured_pipeline_stage,
)
from coinjoin_pipeline.execution.scenarios import container_scenario_path
from coinjoin_pipeline.execution.run_context import (
    detect_active_run as detect_created_run,
)
from coinjoin_pipeline.execution.run_context import (
    is_run_dir as is_pipeline_run_dir,
)
from coinjoin_pipeline.execution.run_context import (
    resolve_run_id as resolve_pipeline_run_id,
)
from coinjoin_pipeline.execution.run_context import (
    run_dirs as pipeline_run_dirs,
)
from coinjoin_pipeline.execution.runtime import (
    CONTAINER_RUNTIME_ENV,
    DEFAULT_CONTAINER_RUNTIME,
    compose_command,
    container_runtime,
)
from coinjoin_pipeline.images import validate_image
from coinjoin_pipeline.storage.s3 import (
    ArtifactTransportError,
    ensure_local_exporters,
    tree_sha256,
)

from . import settings

_CLEANUP_DONE = False


DEFAULT_CONFIGURATION = PipelineConfiguration()


def cleanup_peer_containers() -> None:
    """Stop peer containers and release locks; safe to call more than once."""
    global _CLEANUP_DONE  # pylint: disable=global-statement  # one cleanup per process, shared by both signals
    if _CLEANUP_DONE:
        return
    _CLEANUP_DONE = True
    runtime = os.environ.get(CONTAINER_RUNTIME_ENV, DEFAULT_CONTAINER_RUNTIME)
    if shutil.which(runtime):
        subprocess.run(
            [runtime, "stop", *settings.PEER_CONTAINERS],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if runtime == "podman":
            subprocess.run(
                [runtime, "rm", "-f", "-i", *settings.PEER_CONTAINERS],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    close_pipeline_locks()


def handle_termination(signum: int, _frame: object) -> None:
    """Exit 130 after cleanup, matching the launcher's interrupt contract.

    SIGTERM never unwinds through ``atexit``, so the lock release lived only in
    the launcher's trap until now; both signals route here.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    print(
        f"Interrupted (signal {signum}); stopping CoinJoin analysis containers...",
        file=sys.stderr,
    )
    cleanup_peer_containers()
    sys.exit(130)


def install_termination_handlers() -> None:
    global _CLEANUP_DONE  # pylint: disable=global-statement
    _CLEANUP_DONE = False
    signal.signal(signal.SIGINT, handle_termination)
    signal.signal(signal.SIGTERM, handle_termination)


def run_command(command: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> None:
    """Stream a child command's merged stdout/stderr through the active stage tee."""
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        env=env,
    ) as process:
        assert process.stdout is not None
        with process.stdout:
            for line in process.stdout:
                print(line, end="", flush=True)
        return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)


def default_host_root_dir() -> Path:
    return settings.ROOT_DIR


def compose_project_name(host_root_dir: Path) -> str:
    """Return a stable Compose project name isolated to one pipeline checkout."""
    checkout_hash = hashlib.sha256(str(host_root_dir).encode("utf-8")).hexdigest()[:12]
    return f"{settings.COMPOSE_PROJECT}-{checkout_hash}"


def blocksci_host_port(host_root_dir: Path) -> str:
    """Pick a stable unprivileged notebook port for one pipeline checkout."""
    checkout_hash = hashlib.sha256(str(host_root_dir).encode("utf-8")).hexdigest()
    return str(20000 + int(checkout_hash[:8], 16) % 10000)


def host_registry_config() -> Path | None:
    """Path to the host's Docker registry credentials, when there are any.

    The image prefetch container mounts this so it can warm private base images
    into DinD's cache; DinD carries no credentials of its own, so a build inside
    it fails on a private FROM with "error from registry: denied".
    """
    override = os.environ.get("COINJOIN_REGISTRY_CONFIG")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    docker_config_dir = os.environ.get("DOCKER_CONFIG")
    base = Path(docker_config_dir) if docker_config_dir else Path.home() / ".docker"
    candidate = base.expanduser() / "config.json"
    return candidate if candidate.is_file() else None


def compose_env(
    config: PipelineConfiguration = DEFAULT_CONFIGURATION,
    active_run_id: str | None = None,
    *,
    include_scenario: bool = True,
    create_directories: bool | None = None,
) -> dict[str, str]:
    """Build the Compose environment for one configuration and optional run."""
    if create_directories is None:
        create_directories = not config.dry_run
    env = os.environ.copy()
    host_root_dir = settings.ROOT_DIR
    runs_root = Path(env.get("EMULATION_LOGS_DIR", settings.ROOT_DIR.parent / "coinjoin-runs"))
    scenarios_dir = settings.ROOT_DIR.parent / "scenarios"
    notebooks_dir = runs_root / ".notebooks"
    emulation_logs_dir = runs_root
    exporters_dir = settings.ROOT_DIR / "exporters"
    env.setdefault(settings.COMPOSE_PROJECT_ENV, compose_project_name(host_root_dir))
    env.setdefault("BLOCKSCI_HOST_PORT", blocksci_host_port(host_root_dir))
    env.setdefault("SCENARIOS_DIR", str(scenarios_dir))
    env.setdefault("NOTEBOOKS_DIR", str(notebooks_dir))
    env.setdefault("EMULATION_LOGS_DIR", str(emulation_logs_dir))
    env.setdefault("EXPORTERS_DIR", str(exporters_dir))
    scenarios_dir = Path(env["SCENARIOS_DIR"]).expanduser().resolve()
    notebooks_dir = Path(env["NOTEBOOKS_DIR"]).expanduser().resolve()
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    exporters_dir = Path(env["EXPORTERS_DIR"]).expanduser().resolve()
    env["SCENARIOS_DIR"] = str(scenarios_dir)
    env["NOTEBOOKS_DIR"] = str(notebooks_dir)
    env["EMULATION_LOGS_DIR"] = str(emulation_logs_dir)
    env["EXPORTERS_DIR"] = str(exporters_dir)
    detector = config.joinmarket
    env["COINJOIN_ENGINE"] = config.engine
    env["BLOCKSCI_COINJOIN_TYPE"] = config.coinjoin_type
    env["BLOCKSCI_MIN_INPUT_COUNT"] = "default" if config.min_input_count is None else str(config.min_input_count)
    env["BLOCKSCI_JOINMARKET_DETECTOR"] = detector.detector
    env["BLOCKSCI_JOINMARKET_MIN_BASE_FEE"] = str(detector.min_base_fee)
    env["BLOCKSCI_JOINMARKET_PERCENTAGE_FEE"] = str(detector.percentage_fee)
    env["BLOCKSCI_JOINMARKET_MAX_DEPTH"] = str(detector.max_depth)
    env["RUN_TIMEZONE"] = config.run_timezone
    env.setdefault("BLOCKSCI_IMAGE", settings.DEFAULT_BLOCKSCI_IMAGE)
    env.setdefault("COINJOIN_ANALYSIS_IMAGE", settings.DEFAULT_COINJOIN_ANALYSIS_IMAGE)
    env.setdefault("COINJOIN_EMULATOR_IMAGE", settings.DEFAULT_EMULATOR_IMAGE)
    scenario = config.scenario if include_scenario else None
    env["SCENARIO_FALLBACK_PATH"] = container_scenario_path(scenario, scenarios_dir, config.engine)
    add_image_provenance_env(env)
    if active_run_id:
        env["ACTIVE_RUN_ID"] = active_run_id
        run_dir = emulation_logs_dir / active_run_id
        analysis_dir = run_dir / COINJOIN_ANALYSIS_DIR
        if create_directories:
            analysis_dir.mkdir(parents=True, exist_ok=True)
        env[settings.COINJOIN_ANALYSIS_SOURCE_PATH_ENV] = str(analysis_dir)
        env[settings.COINJOIN_ANALYSIS_MOUNT_PATH_ENV] = (
            f"{settings.COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER}/{active_run_id}"
        )
        env[settings.COINJOIN_ANALYSIS_TARGET_PATH_ENV] = settings.COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER
        env[settings.COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV] = str(run_dir / EMULATOR_DIR / "data")
    else:
        env.pop("ACTIVE_RUN_ID", None)
        env.pop(settings.COINJOIN_ANALYSIS_SOURCE_PATH_ENV, None)
        env.pop(settings.COINJOIN_ANALYSIS_MOUNT_PATH_ENV, None)
        env.pop(settings.COINJOIN_ANALYSIS_TARGET_PATH_ENV, None)
        env.pop(settings.COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV, None)
    registry_config = host_registry_config()
    if registry_config is not None:
        env["COINJOIN_REGISTRY_CONFIG"] = str(registry_config)
    else:
        env.pop("COINJOIN_REGISTRY_CONFIG", None)
    return env


def compose_base_command(env: Mapping[str, str]) -> list[str]:
    """Return the common Compose invocation for this pipeline checkout."""
    return [
        *compose_command(env),
        "-f",
        str(settings.COMPOSE_FILE),
        "-p",
        env.get(settings.COMPOSE_PROJECT_ENV, settings.COMPOSE_PROJECT),
    ]


def inspect_image_provenance(image: str, runtime: str) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            [
                runtime,
                "image",
                "inspect",
                image,
                "--format",
                "{{.Id}}\n{{json .RepoDigests}}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return (None, None)
    lines = result.stdout.splitlines()
    image_id = lines[0].strip() if lines else None
    repo_digest = None
    if len(lines) > 1:
        try:
            repo_digests = json.loads(lines[1])
        except json.JSONDecodeError:
            repo_digests = []
        if repo_digests:
            repo_digest = str(repo_digests[0])
    return (image_id or None, repo_digest)


def add_image_provenance_env(env: dict[str, str]) -> None:
    runtime = container_runtime(env)
    for image_env, (id_env, digest_env) in settings.IMAGE_PROVENANCE_ENV.items():
        image = env.get(image_env)
        if not image:
            continue
        image_id, repo_digest = inspect_image_provenance(image, runtime)
        if image_id:
            env.setdefault(id_env, image_id)
        if repo_digest:
            env.setdefault(digest_env, repo_digest)


def is_run_dir(path: Path) -> bool:
    return is_pipeline_run_dir(path, settings.RUN_MARKER_FILES)


def run_dirs(emulation_logs_dir: Path) -> set[Path]:
    return pipeline_run_dirs(emulation_logs_dir, settings.RUN_MARKER_FILES)


def detect_active_run(emulation_logs_dir: Path, before: set[Path]) -> Path | None:
    return detect_created_run(emulation_logs_dir, before, settings.RUN_MARKER_FILES)


def resolve_run_id(run_dir_arg: str | None, env: dict[str, str]) -> str | None:
    return resolve_pipeline_run_id(
        run_dir_arg,
        Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve(),
        settings.RUN_MARKER_FILES,
    )


def run_script(
    script: Path,
    *args: str,
    config: PipelineConfiguration = DEFAULT_CONFIGURATION,
    active_run_id: str | None = None,
    include_scenario: bool = True,
    blocksci_script: str | None = None,
) -> None:
    if not script.exists():
        print(f"[ERROR] Script not found: {script}", file=sys.stderr)
        sys.exit(1)
    env = compose_env(config, active_run_id, include_scenario=include_scenario, create_directories=True)
    if blocksci_script:
        env["BLOCKSCI_SCRIPT"] = blocksci_script
    try:
        run_command(["bash", str(script), *args], cwd=settings.ROOT_DIR, env=env)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)


def initialize_images() -> None:
    env = compose_env()
    compose_cmd = compose_base_command(env)
    try:
        run_command(
            [*compose_cmd, "--profile", "emulate", "--profile", "analysis", "pull"],
            cwd=settings.ROOT_DIR,
            env=env,
        )
        run_command(
            [
                *compose_cmd,
                "--profile",
                "emulate",
                "run",
                "--rm",
                "dind_image_prefetch",
            ],
            cwd=settings.ROOT_DIR,
            env=env,
        )
        run_command(
            [*compose_cmd, "--profile", "emulate", "down"],
            cwd=settings.ROOT_DIR,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)


def run_coinjoin_analysis(
    run_dir_arg: str | None = None,
    all_runs: bool = False,
    analysis_action: str = "collect_docker",
) -> None:
    env = compose_env()
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"])
    if all_runs:
        active_run_ids = [path.name for path in sorted(run_dirs(emulation_logs_dir))]
    else:
        active_run_id = resolve_run_id(run_dir_arg, env)
        active_run_ids = [active_run_id] if active_run_id else []
    if not active_run_ids:
        print(
            "[ERROR] No grouped emulation run folder found. Run emulate/full-run first or pass --run-dir explicitly.",
            file=sys.stderr,
        )
        sys.exit(2)
    for active_run_id in active_run_ids:
        run_dir = emulation_logs_dir / active_run_id
        baseline_path = run_dir / BASELINE_FILE
        if analysis_action == "analyze_only" and (not baseline_path.is_file()):
            print(
                f"[ERROR] analyze_only requires an existing baseline: {baseline_path}",
                file=sys.stderr,
            )
            sys.exit(2)
        with captured_pipeline_stage(emulation_logs_dir, "coinjoin-analysis baseline", run_dir):
            try:
                run_coinjoin_analysis_docker_stage(active_run_id, analysis_action)
            except subprocess.CalledProcessError as exc:
                sys.exit(exc.returncode)


def run_coinjoin_analysis_docker_stage(active_run_id: str, analysis_action: str = "collect_docker") -> None:
    """Run only coinjoin-analysis through Compose, without starting BlockSci."""
    run_env = compose_env(active_run_id=active_run_id)
    run_env["COINJOIN_ANALYSIS_ACTION"] = analysis_action
    compose_cmd = compose_base_command(run_env)
    run_command(
        [
            *compose_cmd,
            "--profile",
            "analysis",
            "run",
            "--rm",
            "--no-deps",
            "coinjoin_analysis",
        ],
        cwd=settings.ROOT_DIR,
        env=run_env,
    )


def run_blocksci_docker_stage(args: PipelineConfiguration, run_dir: Path, *, include_report: bool) -> None:
    """Run only BlockSci through Compose, optionally deferring the unified report."""
    staged_script = stage_blocksci_script(args.blocksci.script, run_dir)
    env = compose_env(args, run_dir.name)
    env["BLOCKSCI_SCRIPT"] = staged_script or ""
    env["BLOCKSCI_EXPORT_REPORT"] = "true" if include_report else "false"
    compose_cmd = compose_base_command(env)
    run_command(
        [*compose_cmd, "--profile", "analysis", "run", "--rm", "--no-deps", "blocksci"],
        cwd=settings.ROOT_DIR,
        env=env,
    )


def container_run_pull_args(image: str, env_name: str) -> list[str]:
    pull_policy = os.environ.get(env_name)
    if not pull_policy:
        pull_policy = "always" if "/" in image else "missing"
    if pull_policy not in settings.VALID_PULL_POLICIES:
        print(
            f"[ERROR] Invalid {env_name}={pull_policy!r}; expected one of: {', '.join(settings.VALID_PULL_POLICIES)}.",
            file=sys.stderr,
        )
        sys.exit(2)
    return [f"--pull={pull_policy}"]


def exists_or_unreadable(path: Path) -> bool:
    """True when ``path`` is present, or hidden behind a directory we may not read.

    The analysis containers run as root, and ``blocksci_parser`` creates its
    ``parsed/`` directory with mode 0700. The wrapper now runs as the invoking
    user instead of as root inside its own container, so ``Path.is_file()``
    turns the resulting EACCES into a plain ``False`` and a finished BlockSci
    run reads as one that never happened. Everything that consumes these paths
    runs as root in a container, so "cannot look" must not mean "not there".

    ``os.path`` rather than ``Path``: before Python 3.14, ``Path.is_file()`` and
    ``Path.exists()`` raise ``PermissionError`` behind such a directory instead
    of returning ``False``.
    """
    if os.path.isfile(path):
        return True
    for parent in path.parents:
        if not os.path.exists(parent):
            continue
        return not os.access(parent, os.R_OK | os.X_OK)
    return False


def stage_blocksci_script(script: str | None, run_dir: Path) -> str | None:
    """Copy a user analysis script into the run so local and PBS jobs see identical input."""
    if not script:
        return None
    source = Path(script).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"BlockSci script does not exist or is not a file: {source}")
    staged = run_dir / ".pipeline" / "blocksci-script.py"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(source.read_bytes())
    return f"{settings.RUNS_ROOT_CONTAINER}/{run_dir.name}/.pipeline/blocksci-script.py"


def stage_pbs_exporters(run_dir: Path, exporters_dir: Path) -> Path:
    """Snapshot exporters into the shared run directory for PBS compute nodes.

    The bare wrapper executes from a checkout that need not be visible on the
    compute node. Keep the first complete snapshot so retries use the same code
    as the original submission, matching the S3 staged-exporters contract.
    """
    staged = run_dir / ".pipeline" / "exporters"
    try:
        ensure_local_exporters(exporters_dir)
        if staged.exists():
            ensure_local_exporters(staged)
            return staged
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(exporters_dir, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        ensure_local_exporters(staged)
    except (ArtifactTransportError, OSError) as error:
        raise PBSError(f"Failed to stage PBS exporters in {staged}: {error}") from error
    print(f"[stage] PBS exporters staged at {staged} (sha256={tree_sha256(staged)})")
    return staged


def export_preflight_error(coinjoin_ready: bool, blocksci_ready: bool, run_dir: Path) -> str | None:
    if coinjoin_ready and blocksci_ready:
        return None
    missing = []
    if not coinjoin_ready:
        missing.append(str(run_dir / BASELINE_FILE))
    if not blocksci_ready:
        missing.append(str(blocksci_analysis_dir(run_dir) / ARTIFACT_NAME))
    command = "coinjoin-analysis" if not coinjoin_ready and blocksci_ready else "analyze"
    return (
        f"[ERROR] Cannot export unified report for run '{run_dir.name}'; missing analytical input: "
        + ", ".join(missing)
        + f". Run: cjp {command} --run-dir {shlex.quote(run_dir.name)}"
    )


def export_command(active_run_id: str, env: dict[str, str]) -> str:
    command = [
        "python3",
        "/mnt/exporters/worker.py",
        "report",
        "--run-dir",
        f"{settings.RUNS_ROOT_CONTAINER}/{active_run_id}",
        "--scenario",
        env["SCENARIO_FALLBACK_PATH"],
        "--engine",
        env.get("COINJOIN_ENGINE", settings.DEFAULT_ENGINE),
    ]
    for name, key in (
        ("coinjoin-type", "BLOCKSCI_COINJOIN_TYPE"),
        ("min-input-count", "BLOCKSCI_MIN_INPUT_COUNT"),
        ("joinmarket-detector", "BLOCKSCI_JOINMARKET_DETECTOR"),
        ("joinmarket-min-base-fee", "BLOCKSCI_JOINMARKET_MIN_BASE_FEE"),
        ("joinmarket-percentage-fee", "BLOCKSCI_JOINMARKET_PERCENTAGE_FEE"),
        ("joinmarket-max-depth", "BLOCKSCI_JOINMARKET_MAX_DEPTH"),
    ):
        command.extend(["--" + name, env[key]])
    optional_args = [
        ("--blocksci-image", env.get("BLOCKSCI_IMAGE")),
        ("--blocksci-image-id", env.get("BLOCKSCI_IMAGE_ID")),
        ("--blocksci-image-digest", env.get("BLOCKSCI_IMAGE_DIGEST")),
        ("--coinjoin-analysis-image", env.get("COINJOIN_ANALYSIS_IMAGE")),
        ("--coinjoin-analysis-image-id", env.get("COINJOIN_ANALYSIS_IMAGE_ID")),
        ("--coinjoin-analysis-image-digest", env.get("COINJOIN_ANALYSIS_IMAGE_DIGEST")),
        ("--coinjoin-emulator-image", env.get("COINJOIN_EMULATOR_IMAGE")),
        ("--coinjoin-emulator-image-id", env.get("COINJOIN_EMULATOR_IMAGE_ID")),
        ("--coinjoin-emulator-image-digest", env.get("COINJOIN_EMULATOR_IMAGE_DIGEST")),
        ("--uploader-image", env.get("COINJOIN_UPLOADER_IMAGE")),
        ("--unified-report-image", env.get("COINJOIN_UNIFIED_REPORT_IMAGE")),
    ]
    for flag, value in optional_args:
        if value:
            command.extend([flag, value])
    return " ".join((shlex.quote(part) for part in command))


def run_export_only(args: PipelineConfiguration) -> None:
    env = compose_env(args)
    active_run_id = resolve_run_id(args.run_dir, env)
    if not active_run_id:
        print(
            "[ERROR] No emulation run folder found. Run emulate/full-run first, or pass --run-dir explicitly.",
            file=sys.stderr,
        )
        sys.exit(2)
    env = compose_env(args, active_run_id)
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    run_dir = emulation_logs_dir / active_run_id
    coinjoin_ready = exists_or_unreadable(run_dir / BASELINE_FILE)
    blocksci_ready = exists_or_unreadable(blocksci_analysis_dir(run_dir) / ARTIFACT_NAME)
    error = export_preflight_error(coinjoin_ready, blocksci_ready, run_dir)
    if error:
        print(error, file=sys.stderr)
        sys.exit(2)
    image = unified_report_image_reference(args).removeprefix("docker://")
    validate_image(image)
    env["COINJOIN_UNIFIED_REPORT_IMAGE"] = image
    command = [
        container_runtime(),
        "run",
        "--rm",
        *container_run_pull_args(image, "COINJOIN_UNIFIED_REPORT_PULL_POLICY"),
        "-v",
        f"{emulation_logs_dir}:{settings.RUNS_ROOT_CONTAINER}:rw",
        "-v",
        f"{env['EXPORTERS_DIR']}:/mnt/exporters:ro",
        "-v",
        f"{env['SCENARIOS_DIR']}:/mnt/scenarios:ro",
        "--env",
        "REPRODUCTION_COMMAND",
        "--env",
        "COINJOIN_EMULATOR_GIT_COMMIT",
        "--entrypoint",
        "",
        image,
        *shlex.split(export_command(active_run_id, env)),
    ]
    run_command(command, cwd=settings.ROOT_DIR, env=env)
