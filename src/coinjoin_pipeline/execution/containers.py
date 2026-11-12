"""EXPERIMENTAL: Local container commands and their filesystem inputs.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from exporters.analysis_artifact import ARTIFACT_NAME
from exporters.artifact_paths import BASELINE_FILE, COINJOIN_ANALYSIS_DIR, EMULATOR_DIR, blocksci_analysis_dir

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.locks import (
    close_locks as close_pipeline_locks,
)
from coinjoin_pipeline.execution.pbs_settings import resolve_unified_report_image
from coinjoin_pipeline.execution.pipeline_logging import (
    captured_pipeline_stage,
)
from coinjoin_pipeline.execution.run_context import run_dir_under_root, run_dirs
from coinjoin_pipeline.execution.scenarios import container_scenario_path, scenario_source_path
from coinjoin_pipeline.execution.runtime import (
    CONTAINER_RUNTIME_ENV,
    DEFAULT_CONTAINER_RUNTIME,
    compose_command,
    container_runtime,
)
from coinjoin_pipeline.images import validate_image

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

    SIGTERM never unwinds through ``atexit``, so both signals route here to release the locks.
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


def compose_project_name(host_root_dir: Path) -> str:
    """Return a stable Compose project name isolated to one pipeline checkout."""
    checkout_hash = hashlib.sha256(str(host_root_dir).encode("utf-8")).hexdigest()[:12]
    return f"{settings.COMPOSE_PROJECT}-{checkout_hash}"


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
    emulation_logs_dir = runs_root
    exporters_dir = settings.ROOT_DIR / "exporters"
    env.setdefault(settings.COMPOSE_PROJECT_ENV, compose_project_name(host_root_dir))
    env.setdefault("SCENARIOS_DIR", str(scenarios_dir))
    env.setdefault("EMULATION_LOGS_DIR", str(emulation_logs_dir))
    env.setdefault("EXPORTERS_DIR", str(exporters_dir))
    scenarios_dir = Path(env["SCENARIOS_DIR"]).expanduser().resolve()
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    exporters_dir = Path(env["EXPORTERS_DIR"]).expanduser().resolve()
    env["SCENARIOS_DIR"] = str(scenarios_dir)
    env["EMULATION_LOGS_DIR"] = str(emulation_logs_dir)
    env["EXPORTERS_DIR"] = str(exporters_dir)
    env["COINJOIN_ENGINE"] = config.engine
    env["BLOCKSCI_COINJOIN_TYPE"] = config.coinjoin_type
    env["BLOCKSCI_MIN_INPUT_COUNT"] = "default" if config.min_input_count is None else str(config.min_input_count)
    env["RUN_TIMEZONE"] = config.run_timezone
    env.setdefault("BLOCKSCI_IMAGE", settings.DEFAULT_BLOCKSCI_IMAGE)
    env.setdefault("COINJOIN_ANALYSIS_IMAGE", settings.DEFAULT_COINJOIN_ANALYSIS_IMAGE)
    env.setdefault("COINJOIN_EMULATOR_IMAGE", settings.DEFAULT_EMULATOR_IMAGE)
    scenario = config.scenario if include_scenario else None
    if scenario:
        source = scenario_source_path(scenario, scenarios_dir, config.engine)
        if not source.is_relative_to(scenarios_dir):
            scenarios_dir = source.parent
            env["SCENARIOS_DIR"] = str(scenarios_dir)
        scenario = str(source)
    env["SCENARIO_CONTAINER_PATH"] = container_scenario_path(scenario, scenarios_dir, config.engine)
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


def run_script(
    script: Path,
    *args: str,
    config: PipelineConfiguration = DEFAULT_CONFIGURATION,
    active_run_id: str | None = None,
    include_scenario: bool = True,
) -> None:
    if not script.exists():
        print(f"[ERROR] Script not found: {script}", file=sys.stderr)
        sys.exit(1)
    env = compose_env(config, active_run_id, include_scenario=include_scenario, create_directories=True)
    if script == settings.EMULATE_SCRIPT:
        relative_scenario = env["SCENARIO_CONTAINER_PATH"].removeprefix(settings.CONTAINER_SCENARIOS_DIR + "/")
        args = ("--scenario", "/app/scenarios/" + relative_scenario)
    try:
        run_command(["bash", str(script), *args], cwd=settings.ROOT_DIR, env=env)
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
        if not active_run_ids:
            print(f"[ERROR] No emulation run folder found under {emulation_logs_dir}.", file=sys.stderr)
            sys.exit(2)
    else:
        selected = run_dir_under_root(required(run_dir_arg, "run_dir"), emulation_logs_dir.expanduser().resolve())
        active_run_ids = [selected.name]
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
    env = compose_env(args, run_dir.name)
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
    ``parsed/`` directory with mode 0700. This process runs as the invoking user,
    so a lookup there fails with EACCES and a finished BlockSci run would read as
    one that never happened. Everything that consumes these paths
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
    ]
    for name, key in (
        ("coinjoin-type", "BLOCKSCI_COINJOIN_TYPE"),
        ("min-input-count", "BLOCKSCI_MIN_INPUT_COUNT"),
    ):
        command.extend(["--" + name, env[key]])
    return " ".join((shlex.quote(part) for part in command))


def run_export_only(args: PipelineConfiguration) -> None:
    env = compose_env(args)
    logs_root = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    active_run_id = run_dir_under_root(required(args.run_dir, "run_dir"), logs_root).name
    env = compose_env(args, active_run_id)
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    run_dir = emulation_logs_dir / active_run_id
    coinjoin_ready = exists_or_unreadable(run_dir / BASELINE_FILE)
    blocksci_ready = exists_or_unreadable(blocksci_analysis_dir(run_dir) / ARTIFACT_NAME)
    error = export_preflight_error(coinjoin_ready, blocksci_ready, run_dir)
    if error:
        print(error, file=sys.stderr)
        sys.exit(2)
    image = resolve_unified_report_image(args).removeprefix("docker://")
    validate_image(image)
    command = [
        container_runtime(),
        "run",
        "--rm",
        *container_run_pull_args(image, "COINJOIN_UNIFIED_REPORT_PULL_POLICY"),
        "-v",
        f"{emulation_logs_dir}:{settings.RUNS_ROOT_CONTAINER}:rw",
        "-v",
        f"{env['EXPORTERS_DIR']}:/mnt/exporters:ro",
        "--entrypoint",
        "",
        image,
        *shlex.split(export_command(active_run_id, env)),
    ]
    run_command(command, cwd=settings.ROOT_DIR, env=env)
