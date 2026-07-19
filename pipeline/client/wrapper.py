#!/usr/bin/env python3

import argparse
import atexit
import concurrent.futures
import fcntl
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping, TypeVar, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Explicit self-aliases preserve wrapper's historical re-export surface.
# pylint: disable=useless-import-alias,unused-import
from client.artifacts import (
    ArtifactTransportError,
    S3Access,
    ensure_empty_run_prefix,
    s3_access_preflight,
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
    validate_s3_profile,
    wait_for_s3_marker,
)
from client.cli_options import (
    DEFAULT_COINJOIN_TYPE,
    add_coinjoin_type_argument,
    add_dry_run_argument,
    add_engine_argument,
    add_runtime_argument,
)
from client.kubernetes import (
    apply_s3_emulation_resources,
    collect_s3_emulation_diagnostics,
    kubernetes_auth_preflight,
    kubernetes_job_probe,
    kubernetes_s3_auth_preflight,
    render_s3_emulation_resources,
    s3_emulation_job_name,
)
from client.kubernetes import (
    kubectl_auth_can_i as kubectl_auth_can_i,
)
from client.kubernetes import (
    run_kubectl_preflight_command as run_kubectl_preflight_command,
)
from client.pipeline_logging import (
    STAGE_SEPARATOR_WIDTH as STAGE_SEPARATOR_WIDTH,
)
from client.pipeline_logging import (
    StageLog as StageLog,
)
from client.pipeline_logging import (
    TeeStream as TeeStream,
)
from client.pipeline_logging import (
    TerminalColor as TerminalColor,
)
from client.pipeline_logging import (
    captured_pipeline_stage,
)
from client.pipeline_logging import (
    colorize as colorize,
)
from client.pipeline_logging import (
    new_stage_log_path as new_stage_log_path,
)
from client.pipeline_logging import (
    pipeline_stage as pipeline_stage,
)
from client.pipeline_logging import (
    stage_log_slug as stage_log_slug,
)
from client.pipeline_logging import (
    stage_message as stage_message,
)
from client.pipeline_logging import (
    stage_separator as stage_separator,
)
from client.pipeline_logging import (
    terminal_supports_color as terminal_supports_color,
)
from client.runtime import (
    CONTAINER_COMPOSE_COMMAND_ENV as CONTAINER_COMPOSE_COMMAND_ENV,
)
from client.runtime import (
    CONTAINER_RUNTIME_ENV,
    DEFAULT_CONTAINER_RUNTIME,
    compose_command,
    container_runtime,
)
from client.runtime import (
    VALID_CONTAINER_RUNTIMES as VALID_CONTAINER_RUNTIMES,
)
from client.runtime import (
    container_command as container_command,
)

# pylint: enable=useless-import-alias,unused-import

try:
    from client.pbs import (
        DEFAULT_BLOCKSCI_IMAGE as DEFAULT_PBS_BLOCKSCI_IMAGE,
    )
    from client.pbs import (
        DEFAULT_BLOCKSCI_MEM,
        DEFAULT_BLOCKSCI_NCPUS,
        DEFAULT_BLOCKSCI_SCRATCH,
        DEFAULT_BLOCKSCI_WALLTIME,
        DEFAULT_COINJOIN_ANALYSIS_MEM,
        DEFAULT_COINJOIN_ANALYSIS_NCPUS,
        DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
        DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
        DEFAULT_MAPPINGS_ENUMERATOR_IMAGE,
        DEFAULT_SAKE_IMAGE,
        DEFAULT_UNIFIED_REPORT_MEM,
        DEFAULT_UNIFIED_REPORT_NCPUS,
        DEFAULT_UNIFIED_REPORT_SCRATCH,
        DEFAULT_UNIFIED_REPORT_WALLTIME,
        PBSError,
        blocksci_export_pbs_command,
        blocksci_pbs_command,
        coinjoin_analysis_pbs_command,
        pbs_job_probe,
        qdel_pbs_job,
        qdel_pbs_stage,
        require_qsub,
        submit_blocksci_pbs,
        submit_blocksci_s3_pbs,
        submit_coinjoin_analysis_pbs,
        submit_coinjoin_analysis_s3_pbs,
        submit_mappings_pbs,
        submit_unified_report_s3_pbs,
        wait_for_pbs_marker,
        walltime_to_seconds,
    )
    from client.pbs import (
        DEFAULT_COINJOIN_ANALYSIS_IMAGE as DEFAULT_PBS_COINJOIN_ANALYSIS_IMAGE,
    )
except ImportError:
    from pbs import (  # type: ignore[import-not-found,no-redef]
        DEFAULT_BLOCKSCI_IMAGE as DEFAULT_PBS_BLOCKSCI_IMAGE,
    )
    from pbs import (  # type: ignore[no-redef,assignment]
        DEFAULT_BLOCKSCI_MEM,
        DEFAULT_BLOCKSCI_NCPUS,
        DEFAULT_BLOCKSCI_SCRATCH,
        DEFAULT_BLOCKSCI_WALLTIME,
        DEFAULT_COINJOIN_ANALYSIS_MEM,
        DEFAULT_COINJOIN_ANALYSIS_NCPUS,
        DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
        DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
        DEFAULT_MAPPINGS_ENUMERATOR_IMAGE,
        DEFAULT_SAKE_IMAGE,
        DEFAULT_UNIFIED_REPORT_MEM,
        DEFAULT_UNIFIED_REPORT_NCPUS,
        DEFAULT_UNIFIED_REPORT_SCRATCH,
        DEFAULT_UNIFIED_REPORT_WALLTIME,
        PBSError,
        blocksci_export_pbs_command,
        blocksci_pbs_command,
        coinjoin_analysis_pbs_command,
        pbs_job_probe,
        qdel_pbs_job,
        qdel_pbs_stage,
        require_qsub,
        submit_blocksci_pbs,
        submit_blocksci_s3_pbs,
        submit_coinjoin_analysis_pbs,
        submit_coinjoin_analysis_s3_pbs,
        submit_mappings_pbs,
        submit_unified_report_s3_pbs,
        wait_for_pbs_marker,
        walltime_to_seconds,
    )
    from pbs import (  # type: ignore[no-redef]
        DEFAULT_COINJOIN_ANALYSIS_IMAGE as DEFAULT_PBS_COINJOIN_ANALYSIS_IMAGE,
    )
ROOT_DIR = Path(__file__).resolve().parent.parent
CLIENT_DIR = Path(__file__).resolve().parent
RECREATE_SCRIPT = ROOT_DIR / "recreate.sh"
DELETE_SCRIPT = ROOT_DIR / "delete.sh"
ANALYSIS_SCRIPT = ROOT_DIR / "analysis.sh"
COMPOSE_FILE = ROOT_DIR / "compose.yaml"
COMPOSE_PROJECT = "blocksci-emulator"
COINJOIN_ANALYSIS_SOURCE_PATH_ENV = "COINJOIN_ANALYSIS_SOURCE_PATH"
COINJOIN_ANALYSIS_MOUNT_PATH_ENV = "COINJOIN_ANALYSIS_MOUNT_PATH"
COINJOIN_ANALYSIS_TARGET_PATH_ENV = "COINJOIN_ANALYSIS_TARGET_PATH"
COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV = "COINJOIN_ANALYSIS_INPUT_DATA_PATH"
VALID_DRIVERS = ("docker", "kubernetes")
DEFAULT_ACTION = "full-run"
WRAPPER_ACTIONS = (
    "recreate",
    "clean",
    "analyze",
    "export",
    "coinjoin-analysis",
    "coinjoin",
    "mappings",
    "initialize",
    "pbs-from-s3",
    DEFAULT_ACTION,
)
OPTIONS_WITH_VALUES = (
    "--runtime",
    "--scenario",
    "--run-dir",
    "--coinjoin-type",
    "--min-input-count",
    "--engine",
    "--joinmarket-detector",
    "--joinmarket-min-base-fee",
    "--joinmarket-percentage-fee",
    "--joinmarket-max-depth",
    "--driver",
    "--namespace",
    "--kubeconfig",
    "--image-prefix",
    "--run-timezone",
    "--pbs-ncpus",
    "--pbs-mem",
    "--pbs-scratch",
    "--pbs-walltime",
    "--pbs-image",
    "--pbs-blocksci-image",
    "--pbs-coinjoin-analysis-image",
    "--pbs-mappings-enumerator-image",
    "--pbs-sake-image",
    "--mapping-mining-fee-rate",
    "--mapping-coordination-fee-rate",
    "--mapping-max-decomposition-fee",
    "--mapping-mode",
    "--mapping-timeout",
    "--mapping-retry-timeout",
    "--sake-seed",
    "--analysis-action",
    "--pbs-bitcoin-datadir",
    "--blocksci-script",
    "--blocksciScript",
    "--artifact-backend",
    "--artifact-uri",
    "--s3-endpoint-url",
    "--s3-credentials-file",
    "--s3-profile",
    "--s3-secret-name",
    "--run-id",
)
OPTIONS_WITHOUT_VALUES = (
    "--test-values",
    "--coinjoin-infrastructure-local-build",
    "--analysisPbs",
    "--blocksciPbs",
    "--mappingsPbs",
    "--parallel",
)
DEFAULT_DRIVER = "docker"
DEFAULT_ENGINE = "wasabi"
DEFAULT_MIN_INPUT_COUNT: int | None = None
DEFAULT_JOINMARKET_DETECTOR = "definite"
DEFAULT_JOINMARKET_MIN_BASE_FEE = 5000
DEFAULT_JOINMARKET_PERCENTAGE_FEE = 0.00004
DEFAULT_JOINMARKET_MAX_DEPTH = 200000
DEFAULT_BLOCKSCI_IMAGE = "ghcr.io/ondrejman/blocksci-complete:latest"
DEFAULT_COINJOIN_ANALYSIS_IMAGE = "ghcr.io/ondrejman/coinjoin-analysis:latest"
CONTAINER_SCENARIOS_DIR = "/mnt/scenarios"
DEFAULT_CONTAINER_SCENARIO = "/mnt/scenarios/overactive-local.json"
DEFAULT_JOINMARKET_CONTAINER_SCENARIO = "/mnt/scenarios/defaultJoinMarket.json"
DEFAULT_EMULATOR_IMAGE = "ghcr.io/ondrejman/coinjoin-emulator:latest"
DEFAULT_K8S_NAMESPACE = "coinjoin"
DEFAULT_K8S_IMAGE_PREFIX = "ghcr.io/ondrejman/"
DEFAULT_K8S_CONTROL_IP = "host.docker.internal"
DEFAULT_RUN_TIMEZONE = "Europe/Prague"
VALID_PULL_POLICIES = ("always", "missing", "never")
RUNS_ROOT_CONTAINER = "/runs/emulation/logs"
COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER = "/runs/emulation/selected"
RUN_MARKER_FILES = ("coinjoin_emulator_data/scenario.json",)
IMAGE_PROVENANCE_ENV = {
    "BLOCKSCI_IMAGE": ("BLOCKSCI_IMAGE_ID", "BLOCKSCI_IMAGE_DIGEST"),
    "COINJOIN_ANALYSIS_IMAGE": ("COINJOIN_ANALYSIS_IMAGE_ID", "COINJOIN_ANALYSIS_IMAGE_DIGEST"),
    "COINJOIN_EMULATOR_IMAGE": ("COINJOIN_EMULATOR_IMAGE_ID", "COINJOIN_EMULATOR_IMAGE_DIGEST"),
    "WRAPPER_IMAGE": ("WRAPPER_IMAGE_ID", "WRAPPER_IMAGE_DIGEST"),
}


def acquire_lock(path: Path) -> object:
    """Acquire a non-blocking advisory lock that is released on process exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError(f"Another pipeline command holds lock: {path}") from error
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    atexit.register(handle.close)
    return handle


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


def normalize_argv(argv: list[str]) -> list[str]:
    """Default to full-run when CLI options are provided without an action."""
    if any(arg in ("-h", "--help") for arg in argv):
        return argv

    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in OPTIONS_WITH_VALUES:
            skip_next = True
            continue
        if any(arg.startswith(f"{option}=") for option in OPTIONS_WITH_VALUES):
            continue
        if arg in OPTIONS_WITHOUT_VALUES:
            continue
        if arg.startswith("-"):
            continue
        if arg in WRAPPER_ACTIONS:
            return argv
        break

    return [DEFAULT_ACTION, *argv]


def run_timezone(value: str) -> str:
    """Validate an IANA timezone used to name newly created emulator runs."""
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise argparse.ArgumentTypeError(f"unknown IANA timezone: {value}") from error
    return value


def positive_int(value: str) -> int:
    """Parse a strictly positive command-line integer."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def default_host_root_dir() -> Path:
    host_client_dir = os.environ.get("HOST_CLIENT_DIR")
    if host_client_dir:
        return Path(host_client_dir).expanduser().resolve().parent
    return ROOT_DIR


def compose_env(
    active_run_id: str | None = None,
    engine: str = DEFAULT_ENGINE,
    coinjoin_type: str = DEFAULT_COINJOIN_TYPE,
    min_input_count: int | None = DEFAULT_MIN_INPUT_COUNT,
    scenario: str | None = None,
    test_values: bool = False,
    joinmarket_detector: str = DEFAULT_JOINMARKET_DETECTOR,
    joinmarket_min_base_fee: int = DEFAULT_JOINMARKET_MIN_BASE_FEE,
    joinmarket_percentage_fee: float = DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    joinmarket_max_depth: int = DEFAULT_JOINMARKET_MAX_DEPTH,
    run_timezone_name: str = DEFAULT_RUN_TIMEZONE,
) -> dict[str, str]:
    env = os.environ.copy()
    uses_host_paths = "HOST_CLIENT_DIR" in os.environ
    host_root_dir = default_host_root_dir()
    scenarios_dir = host_root_dir / "client" / "scenarios"
    notebooks_dir = host_root_dir / "client" / "notebooks"
    emulation_logs_dir = host_root_dir / "emulation_logs"
    exporters_dir = host_root_dir / "exporters"

    if not uses_host_paths:
        scenarios_dir.mkdir(parents=True, exist_ok=True)
        notebooks_dir.mkdir(parents=True, exist_ok=True)
        emulation_logs_dir.mkdir(parents=True, exist_ok=True)

    env.setdefault("HOST_CLIENT_DIR", str(host_root_dir / "client"))
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
    env["COINJOIN_ENGINE"] = engine
    env["BLOCKSCI_COINJOIN_TYPE"] = coinjoin_type
    env["BLOCKSCI_MIN_INPUT_COUNT"] = "default" if min_input_count is None else str(min_input_count)
    env["BLOCKSCI_TEST_VALUES"] = "true" if test_values else "false"
    env["BLOCKSCI_JOINMARKET_DETECTOR"] = joinmarket_detector
    env["BLOCKSCI_JOINMARKET_MIN_BASE_FEE"] = str(joinmarket_min_base_fee)
    env["BLOCKSCI_JOINMARKET_PERCENTAGE_FEE"] = str(joinmarket_percentage_fee)
    env["BLOCKSCI_JOINMARKET_MAX_DEPTH"] = str(joinmarket_max_depth)
    env["RUN_TIMEZONE"] = run_timezone_name
    env.setdefault("BLOCKSCI_IMAGE", DEFAULT_BLOCKSCI_IMAGE)
    env.setdefault("COINJOIN_ANALYSIS_IMAGE", DEFAULT_COINJOIN_ANALYSIS_IMAGE)
    env.setdefault("COINJOIN_EMULATOR_IMAGE", DEFAULT_EMULATOR_IMAGE)
    env["SCENARIO_FALLBACK_PATH"] = container_scenario_path(scenario, scenarios_dir, engine)
    add_image_provenance_env(env)
    if active_run_id:
        env["ACTIVE_RUN_ID"] = active_run_id
        run_dir = emulation_logs_dir / active_run_id
        analysis_dir = run_dir / "coinjoin-analysis_data"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        env[COINJOIN_ANALYSIS_SOURCE_PATH_ENV] = str(analysis_dir)
        env[COINJOIN_ANALYSIS_MOUNT_PATH_ENV] = f"{COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER}/{active_run_id}"
        env[COINJOIN_ANALYSIS_TARGET_PATH_ENV] = COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER
        env[COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV] = str(run_dir / "coinjoin_emulator_data" / "data")
    else:
        env.pop("ACTIVE_RUN_ID", None)
        env.pop(COINJOIN_ANALYSIS_SOURCE_PATH_ENV, None)
        env.pop(COINJOIN_ANALYSIS_MOUNT_PATH_ENV, None)
        env.pop(COINJOIN_ANALYSIS_TARGET_PATH_ENV, None)
        env.pop(COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV, None)
    return env


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
        return None, None

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
    return image_id or None, repo_digest


def add_image_provenance_env(env: dict[str, str]) -> None:
    runtime = container_runtime(env)
    for image_env, (id_env, digest_env) in IMAGE_PROVENANCE_ENV.items():
        image = env.get(image_env)
        if not image:
            continue
        image_id, repo_digest = inspect_image_provenance(image, runtime)
        if image_id:
            env.setdefault(id_env, image_id)
        if repo_digest:
            env.setdefault(digest_env, repo_digest)


def default_container_scenario(engine: str) -> str:
    return DEFAULT_JOINMARKET_CONTAINER_SCENARIO if engine == "joinmarket" else DEFAULT_CONTAINER_SCENARIO


def container_scenario_path(scenario: str | None, scenarios_dir: Path, engine: str = DEFAULT_ENGINE) -> str:
    if not scenario:
        return default_container_scenario(engine)

    scenario_path = Path(scenario).expanduser()
    if not scenario_path.is_absolute():
        parts = scenario_path.parts
        if len(parts) >= 2 and parts[0] == "client" and parts[1] == "scenarios":
            scenario_path = scenarios_dir.joinpath(*parts[2:])
        elif len(parts) >= 1 and parts[0] == "scenarios":
            scenario_path = scenarios_dir.joinpath(*parts[1:])
        else:
            scenario_path = scenarios_dir / scenario_path
    scenario_path = scenario_path.resolve()

    try:
        relative_path = scenario_path.relative_to(scenarios_dir.resolve())
    except ValueError:
        # Substituting the default here would run a different experiment than the
        # one requested while the evidence recorded the substitute as genuine.
        print(
            f"[ERROR] Scenario {scenario_path} is outside the scenarios directory "
            f"{scenarios_dir}, so it cannot be mounted into the container. "
            f"Copy it under {scenarios_dir} and pass it by name.",
            file=sys.stderr,
        )
        sys.exit(2)

    return CONTAINER_SCENARIOS_DIR + "/" + str(relative_path).replace(os.sep, "/")


def host_scenario_path(container_scenario: str, scenarios_dir: Path) -> Path:
    """Map a container scenario path back to its host path, preserving nesting."""
    relative_path = container_scenario.removeprefix(CONTAINER_SCENARIOS_DIR + "/")
    return scenarios_dir.joinpath(*relative_path.split("/"))


def is_run_dir(path: Path) -> bool:
    return path.is_dir() and any((path / marker).exists() for marker in RUN_MARKER_FILES)


def run_dirs(emulation_logs_dir: Path) -> set[Path]:
    if not emulation_logs_dir.exists():
        return set()
    return {child.resolve() for child in emulation_logs_dir.iterdir() if is_run_dir(child)}


def newest_run_dir(emulation_logs_dir: Path) -> Path | None:
    candidates = run_dirs(emulation_logs_dir)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def pipeline_run_id_env() -> str:
    """Run id the host CLI chose for this emulation, forwarded via PIPELINE_RUN_ID."""
    run_id_value = os.environ.get("PIPELINE_RUN_ID", "")
    if not run_id_value:
        return ""
    try:
        return validate_run_id(run_id_value)
    except ValueError as error:
        print(f"[WARN] Ignoring invalid PIPELINE_RUN_ID: {error}", file=sys.stderr)
        return ""


def detect_active_run(emulation_logs_dir: Path, before: set[Path]) -> Path | None:
    """Locate the run directory produced by the emulation that just finished.

    When the host CLI pinned the run id, only that directory counts; a stale
    older run must not be silently analyzed in its place.
    """
    expected_run_id = pipeline_run_id_env()
    if expected_run_id:
        run_dir = (emulation_logs_dir / expected_run_id).resolve()
        return run_dir if is_run_dir(run_dir) else None
    created = sorted(run_dirs(emulation_logs_dir) - before, key=lambda path: path.stat().st_mtime)
    if created:
        return created[-1]
    return newest_run_dir(emulation_logs_dir)


def run_dir_under_root(run_dir_arg: str, runs_root: Path) -> Path:
    """Resolve --run-dir to an existing run directly under the runs root.

    Callers only ever keep the basename and rejoin it with the runs root, so a
    path pointing elsewhere would silently act on a same-named run instead.
    """
    requested = Path(run_dir_arg).expanduser()
    resolved = requested.resolve() if requested.is_absolute() else (runs_root / requested).resolve()
    if resolved.parent != runs_root:
        print(
            f"[ERROR] --run-dir must name a run inside the runs root {runs_root}, "
            f"but {resolved} is outside it.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not resolved.exists():
        print(f"[ERROR] Run directory not found: {resolved}", file=sys.stderr)
        sys.exit(2)
    return resolved


def resolve_run_id(run_dir_arg: str | None, env: dict[str, str]) -> str | None:
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    if run_dir_arg:
        return run_dir_under_root(run_dir_arg, emulation_logs_dir).name

    latest = newest_run_dir(emulation_logs_dir)
    if latest is None:
        return None
    print(f"[WARN] No --run-dir provided; using newest run folder: {latest}", file=sys.stderr)
    return latest.name


def run_script(
    script: Path,
    *args: str,
    active_run_id: str | None = None,
    engine: str = DEFAULT_ENGINE,
    coinjoin_type: str = DEFAULT_COINJOIN_TYPE,
    min_input_count: int | None = DEFAULT_MIN_INPUT_COUNT,
    scenario: str | None = None,
    test_values: bool = False,
    joinmarket_detector: str = DEFAULT_JOINMARKET_DETECTOR,
    joinmarket_min_base_fee: int = DEFAULT_JOINMARKET_MIN_BASE_FEE,
    joinmarket_percentage_fee: float = DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    joinmarket_max_depth: int = DEFAULT_JOINMARKET_MAX_DEPTH,
    run_timezone_name: str = DEFAULT_RUN_TIMEZONE,
    blocksci_script: str | None = None,
) -> None:
    if not script.exists():
        print(f"[ERROR] Script not found: {script}", file=sys.stderr)
        sys.exit(1)
    env = compose_env(
        active_run_id,
        engine,
        coinjoin_type,
        min_input_count,
        scenario,
        test_values,
        joinmarket_detector,
        joinmarket_min_base_fee,
        joinmarket_percentage_fee,
        joinmarket_max_depth,
        run_timezone_name,
    )
    if blocksci_script:
        env["BLOCKSCI_SCRIPT"] = blocksci_script
    try:
        run_command(["bash", str(script), *args], cwd=ROOT_DIR, env=env)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)


def initialize_images() -> None:
    env = compose_env()
    compose_cmd = [
        *compose_command(env),
        "-f",
        str(COMPOSE_FILE),
        "-p",
        COMPOSE_PROJECT,
    ]
    try:
        # 1. Pull the outer compose images
        run_command(
            [*compose_cmd, "--profile", "recreate", "--profile", "analysis", "pull"],
            cwd=CLIENT_DIR,
            env=env,
        )

        # 2. Run the prefetch task.
        # By removing --no-deps, Compose will automatically start 'dind' and wait
        # for it to be healthy before executing the prefetch commands.
        run_command(
            [*compose_cmd, "--profile", "recreate", "run", "--rm", "dind_image_prefetch"],
            cwd=CLIENT_DIR,
            env=env,
        )

        # 3. Cleanup transient services and the DinD daemon
        run_command(
            [*compose_cmd, "--profile", "recreate", "down"],
            cwd=CLIENT_DIR,
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
            "[ERROR] No grouped emulation run folder found. Run recreate/full-run first or pass --run-dir explicitly.",
            file=sys.stderr,
        )
        sys.exit(2)

    for active_run_id in active_run_ids:
        run_dir = emulation_logs_dir / active_run_id
        baseline_path = run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json"
        if analysis_action == "analyze_only" and not baseline_path.is_file():
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


def run_coinjoin_analysis_docker_stage(
    active_run_id: str,
    analysis_action: str = "collect_docker",
) -> None:
    """Run only coinjoin-analysis through Compose, without starting BlockSci."""
    run_env = compose_env(active_run_id)
    run_env["COINJOIN_ANALYSIS_ACTION"] = analysis_action
    compose_cmd = [
        *compose_command(run_env),
        "-f",
        str(COMPOSE_FILE),
        "-p",
        COMPOSE_PROJECT,
    ]
    run_command(
        [*compose_cmd, "--profile", "analysis", "run", "--rm", "--no-deps", "coinjoin_analysis"],
        cwd=CLIENT_DIR,
        env=run_env,
    )


def run_blocksci_docker_stage(args: argparse.Namespace, run_dir: Path, *, include_report: bool) -> None:
    """Run only BlockSci through Compose, optionally deferring the unified report."""
    staged_script = stage_blocksci_script(getattr(args, "blocksci_script", None), run_dir)
    env = compose_env(
        run_dir.name,
        args.engine,
        args.coinjoin_type,
        args.min_input_count,
        args.scenario,
        args.test_values,
        args.joinmarket_detector,
        args.joinmarket_min_base_fee,
        args.joinmarket_percentage_fee,
        args.joinmarket_max_depth,
    )
    env["BLOCKSCI_SCRIPT"] = staged_script or ""
    env["BLOCKSCI_EXPORT_REPORT"] = "true" if include_report else "false"
    compose_cmd = [
        *compose_command(env),
        "-f",
        str(COMPOSE_FILE),
        "-p",
        COMPOSE_PROJECT,
    ]
    run_command(
        [*compose_cmd, "--profile", "analysis", "run", "--rm", "--no-deps", "blocksci"],
        cwd=CLIENT_DIR,
        env=env,
    )


def run_kubernetes_emulation(
    scenario: str | None = None,
    engine: str = DEFAULT_ENGINE,
    namespace: str = DEFAULT_K8S_NAMESPACE,
    reuse_namespace: bool = False,
    image_prefix: str = DEFAULT_K8S_IMAGE_PREFIX,
    kubeconfig: str | None = None,
    coinjoin_infrastructure_local_build: bool = False,
    run_timezone_name: str = DEFAULT_RUN_TIMEZONE,
    kubernetes_btc_datadir: str | None = None,
    copy_to_host: bool = False,
    prepare_local_analysis: bool = True,
) -> None:
    """Run the coinjoin emulation on a Kubernetes cluster.

    Instead of using Docker-in-Docker via compose, this directly runs the
    coinjoin-emulator container image with ``--driver kubernetes``.  The
    emulator connects to the Kubernetes cluster (via the mounted kubeconfig)
    and creates pods for btc-node, wasabi-backend, wasabi-coordinator,
    wasabi-clients, etc.

    By default the btc-node pod writes directly to a shared host path. The
    legacy Kubernetes API download remains available through ``copy_to_host``.
    """
    env = compose_env(engine=engine, scenario=scenario, run_timezone_name=run_timezone_name)
    host_root_dir = default_host_root_dir()
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    scenarios_dir = Path(env["SCENARIOS_DIR"]).expanduser().resolve()
    copy_to_host_dir = os.environ.get("KUBERNETES_COPY_TO_HOST_DIR")
    if copy_to_host and not copy_to_host_dir:
        print(
            "[ERROR] --copy-to-host requires KUBERNETES_COPY_TO_HOST_DIR; "
            "the launcher must mount an explicit host-owned output directory.",
            file=sys.stderr,
        )
        sys.exit(2)
    local_btc_data_dir = Path(
        copy_to_host_dir or host_root_dir / "btc-data"
    ).expanduser().resolve()
    local_download_path = local_btc_data_dir / "data"
    shared_btc_data_path = Path(kubernetes_btc_datadir or local_download_path).expanduser().resolve()

    emulation_logs_dir.mkdir(parents=True, exist_ok=True)
    if copy_to_host:
        local_btc_data_dir.mkdir(parents=True, exist_ok=True)

    # Resolve the scenario path for the emulator container
    if scenario:
        container_scenario = container_scenario_path(scenario, scenarios_dir, engine)
    else:
        container_scenario = default_container_scenario(engine)

    # Resolve kubeconfig
    if kubeconfig:
        kubeconfig_path = Path(kubeconfig).expanduser().resolve()
    else:
        kubeconfig_path = Path.home() / ".kube" / "config"

    if not kubeconfig_path.exists():
        print(
            f"[ERROR] Kubeconfig not found at {kubeconfig_path}. Pass --kubeconfig or ensure ~/.kube/config exists.",
            file=sys.stderr,
        )
        sys.exit(2)

    kubernetes_auth_preflight(kubeconfig_path, namespace, reuse_namespace)

    emulator_cmd = kubernetes_emulator_command(
        scenario=container_scenario,
        engine=engine,
        namespace=namespace,
        reuse_namespace=reuse_namespace,
        image_prefix=image_prefix,
        btc_data_path=("/btc-data/data" if copy_to_host else str(shared_btc_data_path)),
        copy_to_host=copy_to_host,
        control_ip=os.environ.get("KUBERNETES_CONTROL_IP", DEFAULT_K8S_CONTROL_IP),
        coinjoin_infrastructure_local_build=coinjoin_infrastructure_local_build,
        run_timezone_name=run_timezone_name,
    )

    # Run the emulator container locally, with kubeconfig mounted so it
    # can reach the Kubernetes cluster
    runtime = container_runtime()
    emulator_image = os.environ.get("COINJOIN_EMULATOR_IMAGE", DEFAULT_EMULATOR_IMAGE)
    storage_uid = os.environ.get("KUBERNETES_STORAGE_UID", str(os.getuid()))
    storage_gid = os.environ.get("KUBERNETES_STORAGE_GID", str(os.getgid()))
    docker_cmd = [
        runtime,
        "run",
        "--rm",
        *container_run_pull_args(emulator_image, "COINJOIN_EMULATOR_PULL_POLICY"),
        "--user",
        f"{storage_uid}:{storage_gid}",
        "-v",
        f"{kubeconfig_path}:/tmp/coinjoin-kubeconfig:ro",
        "-v",
        f"{scenarios_dir}:/mnt/scenarios:ro",
        "-v",
        f"{emulation_logs_dir}:/app/logs:rw",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "HOME=/tmp",
        "-e",
        "KUBECONFIG=/tmp/coinjoin-kubeconfig",
    ]
    if copy_to_host:
        docker_cmd.extend(["-v", f"{local_btc_data_dir}:/btc-data:rw"])
    else:
        docker_cmd.extend(["-e", f"KUBERNETES_STORAGE_UID={storage_uid}"])
        docker_cmd.extend(["-e", f"KUBERNETES_STORAGE_GID={storage_gid}"])
    docker_cmd.extend([emulator_image, *emulator_cmd])

    print(f"[kubernetes] Running emulator with driver=kubernetes, namespace={namespace}")
    print(f"[kubernetes] Scenario: {container_scenario}")
    print(f"[kubernetes] Kubeconfig: {kubeconfig_path}")
    transfer_mode = "copy to host" if copy_to_host else "direct shared mount"
    print(f"[kubernetes] BTC data mode: {transfer_mode}")
    print(f"[kubernetes] BTC data output: {local_download_path if copy_to_host else shared_btc_data_path}")
    print(f"[kubernetes] Control IP: {os.environ.get('KUBERNETES_CONTROL_IP', DEFAULT_K8S_CONTROL_IP)}")

    try:
        run_command(docker_cmd, env=os.environ.copy())
    except subprocess.CalledProcessError as exc:
        print(
            f"[ERROR] Kubernetes emulation failed with exit code {exc.returncode}",
            file=sys.stderr,
        )
        sys.exit(exc.returncode)

    if prepare_local_analysis:
        populate_btc_data_volume(local_download_path if copy_to_host else shared_btc_data_path)
    print("[kubernetes] Emulation complete. BTC data ready for analysis.")


def container_run_pull_args(image: str, env_name: str) -> list[str]:
    pull_policy = os.environ.get(env_name)
    if not pull_policy:
        pull_policy = "always" if "/" in image else "missing"
    if pull_policy not in VALID_PULL_POLICIES:
        print(
            f"[ERROR] Invalid {env_name}={pull_policy!r}; expected one of: {', '.join(VALID_PULL_POLICIES)}.",
            file=sys.stderr,
        )
        sys.exit(2)
    return [f"--pull={pull_policy}"]


def kubernetes_emulator_command(
    scenario: str,
    engine: str = DEFAULT_ENGINE,
    namespace: str = DEFAULT_K8S_NAMESPACE,
    reuse_namespace: bool = False,
    image_prefix: str = DEFAULT_K8S_IMAGE_PREFIX,
    btc_data_path: str = "/btc-data/data",
    copy_to_host: bool = False,
    control_ip: str = DEFAULT_K8S_CONTROL_IP,
    coinjoin_infrastructure_local_build: bool = False,
    run_timezone_name: str = DEFAULT_RUN_TIMEZONE,
) -> list[str]:
    """Build the coinjoin-emulator command for Kubernetes mode."""
    command = [
        "python",
        "manager.py",
        "--engine",
        engine,
        "--driver",
        "kubernetes",
        "--run-timezone",
        run_timezone_name,
        "run",
        "--scenario",
        scenario,
        "--namespace",
        namespace,
        "--image-prefix",
        image_prefix,
        "--control-ip",
        control_ip,
        "--btc-node-arg=-blocksxor=0",
    ]
    pinned_run_id = pipeline_run_id_env()
    if pinned_run_id:
        command.extend(["--run-id", pinned_run_id])
    if engine == "joinmarket":
        command.append("--joinmarket-descriptor-regtest-fallback")
    if copy_to_host:
        command.extend(["--download-btc-data", btc_data_path])
    else:
        command.extend(["--btcFolder", btc_data_path])
    if coinjoin_infrastructure_local_build:
        command.append("--coinjoin-infrastructure-local-build")
    if reuse_namespace:
        command.append("--reuse-namespace")
    return command


def populate_btc_data_volume(btc_data_dir: Path) -> None:
    """Copy downloaded btc-data into the Docker named volume used by blocksci.

    The analysis compose services expect blockchain data in the
    ``blocksci-emulator_btc_data`` named Docker volume.  After a Kubernetes
    emulation run, the data lives in a local directory.  This helper copies
    it into the volume so that the existing analysis pipeline works
    unchanged.
    """
    volume_name = f"{COMPOSE_PROJECT}_btc_data"
    runtime = container_runtime()
    # Reuse the wrapper image for the copy helper instead of pulling an
    # unpinned `alpine`; it is already present and provides sh/cp.
    helper_image = os.environ.get("WRAPPER_IMAGE", "ghcr.io/ondrejman/coinjoin-pipeline:latest")

    # Ensure the volume exists
    subprocess.run(
        [runtime, "volume", "create", volume_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print(f"[kubernetes] Populating {runtime} volume '{volume_name}' with btc-data...")
    try:
        run_command(
            [
                runtime,
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                "-v",
                f"{volume_name}:/vol:rw",
                "-v",
                f"{btc_data_dir}:/src:ro",
                helper_image,
                "-c",
                "cp -a /src/. /vol/",
            ],
        )
        print(f"[kubernetes] Volume '{volume_name}' populated successfully.")
    except subprocess.CalledProcessError as exc:
        print(
            f"[WARN] Could not populate btc_data volume: {exc}",
            file=sys.stderr,
        )


def blocksci_config_path(run_dir: Path) -> Path:
    return run_dir / "blocksci_data" / "config.json"


def blocksci_parsed_chain_path(run_dir: Path) -> Path:
    return run_dir / "blocksci_data" / "parsed" / "chain" / "block.dat"


def blocksci_container_config_path(active_run_id: str) -> str:
    return f"{RUNS_ROOT_CONTAINER}/{active_run_id}/blocksci_data/config.json"


def blocksci_output_exists(run_dir: Path) -> bool:
    return blocksci_config_path(run_dir).is_file() and blocksci_parsed_chain_path(run_dir).is_file()


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
    return f"{RUNS_ROOT_CONTAINER}/{run_dir.name}/.pipeline/blocksci-script.py"


def export_preflight_error(
    coinjoin_ready: bool,
    blocksci_ready: bool,
    run_dir: Path,
) -> str | None:
    if coinjoin_ready and blocksci_ready:
        return None

    coinjoin_path = run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json"
    blocksci_location = str(blocksci_config_path(run_dir))
    parsed_chain_location = str(blocksci_parsed_chain_path(run_dir))

    if not coinjoin_ready and not blocksci_ready:
        return (
            f"[ERROR] Cannot export unified report for run '{run_dir.name}': "
            "neither prerequisite is ready.\n"
            f"Missing CoinJoin output: {coinjoin_path}\n"
            f"Missing BlockSci run output: {blocksci_location} or {parsed_chain_location}\n"
            "Run the full pipeline first, or run recreate/analyze before export."
        )

    if not coinjoin_ready:
        return (
            f"[ERROR] BlockSci run output exists, but CoinJoin output is missing for run "
            f"'{run_dir.name}'.\n"
            f"Missing CoinJoin output: {coinjoin_path}\n"
            f"Run: python3 client/wrapper.py coinjoin-analysis --run-dir {shlex.quote(run_dir.name)}"
        )

    return (
        f"[ERROR] CoinJoin output exists, but BlockSci run output is missing for run "
        f"'{run_dir.name}'.\n"
        f"Missing BlockSci run output: {blocksci_location} or {parsed_chain_location}\n"
        f"Run: python3 client/wrapper.py analyze --run-dir {shlex.quote(run_dir.name)}"
    )


def export_command(active_run_id: str, env: dict[str, str]) -> str:
    command = [
        "python3",
        "/mnt/exporters/unified_report.py",
        "--config",
        blocksci_container_config_path(active_run_id),
        "--runs-root",
        RUNS_ROOT_CONTAINER,
        "--run-dir",
        f"{RUNS_ROOT_CONTAINER}/{active_run_id}",
        "--scenario",
        env["SCENARIO_FALLBACK_PATH"],
        "--engine",
        env.get("COINJOIN_ENGINE", DEFAULT_ENGINE),
        "--coinjoin-type",
        env["BLOCKSCI_COINJOIN_TYPE"],
        "--min-input-count",
        env["BLOCKSCI_MIN_INPUT_COUNT"],
        "--joinmarket-detector",
        env["BLOCKSCI_JOINMARKET_DETECTOR"],
        "--joinmarket-min-base-fee",
        env["BLOCKSCI_JOINMARKET_MIN_BASE_FEE"],
        "--joinmarket-percentage-fee",
        env["BLOCKSCI_JOINMARKET_PERCENTAGE_FEE"],
        "--joinmarket-max-depth",
        env["BLOCKSCI_JOINMARKET_MAX_DEPTH"],
        "--markdown",
    ]
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
        ("--wrapper-image", env.get("WRAPPER_IMAGE")),
        ("--wrapper-image-id", env.get("WRAPPER_IMAGE_ID")),
        ("--wrapper-image-digest", env.get("WRAPPER_IMAGE_DIGEST")),
    ]
    for flag, value in optional_args:
        if value:
            command.extend([flag, value])
    if env.get("BLOCKSCI_TEST_VALUES") == "true":
        command.append("--test-values")
    return " ".join(shlex.quote(part) for part in command)


def run_export_only(args: argparse.Namespace) -> None:
    env = compose_env(
        None,
        args.engine,
        args.coinjoin_type,
        args.min_input_count,
        args.scenario,
        args.test_values,
        args.joinmarket_detector,
        args.joinmarket_min_base_fee,
        args.joinmarket_percentage_fee,
        args.joinmarket_max_depth,
    )
    active_run_id = resolve_run_id(args.run_dir, env)
    if not active_run_id:
        print(
            "[ERROR] No emulation run folder found. Run recreate/full-run first, or pass --run-dir explicitly.",
            file=sys.stderr,
        )
        sys.exit(2)

    env = compose_env(
        active_run_id,
        args.engine,
        args.coinjoin_type,
        args.min_input_count,
        args.scenario,
        args.test_values,
        args.joinmarket_detector,
        args.joinmarket_min_base_fee,
        args.joinmarket_percentage_fee,
        args.joinmarket_max_depth,
    )
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    run_dir = emulation_logs_dir / active_run_id
    coinjoin_ready = (run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json").exists()
    blocksci_ready = blocksci_output_exists(run_dir)

    error = export_preflight_error(
        coinjoin_ready,
        blocksci_ready,
        run_dir,
    )
    if error:
        print(error, file=sys.stderr)
        sys.exit(2)

    compose_cmd = [
        *compose_command(env),
        "-f",
        str(COMPOSE_FILE),
        "-p",
        COMPOSE_PROJECT,
    ]
    try:
        run_command(
            [
                *compose_cmd,
                "--profile",
                "analysis",
                "run",
                "--rm",
                "--no-deps",
                "blocksci",
                "-c",
                export_command(active_run_id, env),
            ],
            cwd=CLIENT_DIR,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)


def add_kubernetes_arguments(arg_parser: argparse.ArgumentParser) -> None:
    """Add Kubernetes-specific arguments to a subparser."""
    arg_parser.add_argument(
        "--driver",
        choices=VALID_DRIVERS,
        default=DEFAULT_DRIVER,
        help=(
            "Container orchestration driver for the emulation step. "
            "'docker' uses Docker-in-Docker via compose (default). "
            "'kubernetes' runs the emulation pods on a Kubernetes cluster."
        ),
    )
    arg_parser.add_argument(
        "--namespace",
        type=str,
        default=DEFAULT_K8S_NAMESPACE,
        help=f"Kubernetes namespace for emulation pods (default: {DEFAULT_K8S_NAMESPACE}).",
    )
    arg_parser.add_argument(
        "--reuse-namespace",
        action="store_true",
        default=False,
        help="Reuse an existing Kubernetes namespace instead of creating a new one.",
    )
    arg_parser.add_argument(
        "--kubeconfig",
        type=str,
        default=None,
        help="Path to kubeconfig file (default: ~/.kube/config).",
    )
    arg_parser.add_argument(
        "--image-prefix",
        type=str,
        default=DEFAULT_K8S_IMAGE_PREFIX,
        help=f"Image registry prefix for Kubernetes pods (default: {DEFAULT_K8S_IMAGE_PREFIX}).",
    )
    arg_parser.add_argument(
        "--kubernetes-btc-datadir",
        type=str,
        default=None,
        help=(
            "Shared absolute Bitcoin Core datadir mounted directly into the "
            "Kubernetes btc-node pod. Defaults to --pbs-bitcoin-datadir when set."
        ),
    )
    arg_parser.add_argument(
        "--copy-to-host",
        action="store_true",
        default=False,
        help=("Use the legacy pod-to-wrapper Bitcoin datadir download instead of writing directly to shared storage."),
    )
    add_emulator_infrastructure_image_arguments(arg_parser)


def add_artifact_arguments(
    arg_parser: argparse.ArgumentParser,
    *,
    pbs_credentials: bool = False,
    kubernetes_secret: bool = False,
) -> None:
    arg_parser.add_argument(
        "--artifact-backend",
        choices=("shared-storage", "s3"),
        default="shared-storage",
        help="Artifact transport backend (default: shared-storage).",
    )
    arg_parser.add_argument("--artifact-uri", help="S3-compatible run prefix, for example s3://bucket/runs.")
    arg_parser.add_argument("--s3-endpoint-url", help="CESNET/MetaCentrum S3-compatible endpoint URL.")
    arg_parser.add_argument("--run-id", help="Deterministic artifact run identifier.")
    if pbs_credentials:
        arg_parser.add_argument("--s3-credentials-file", help="Absolute s5cmd credentials-file path for PBS jobs.")
        arg_parser.add_argument("--s3-profile", help="Named profile in the s5cmd credentials file.")
    if kubernetes_secret:
        arg_parser.add_argument("--s3-secret-name", help="Pre-created Kubernetes Secret for S3-compatible upload.")


def add_emulator_infrastructure_image_arguments(arg_parser: argparse.ArgumentParser) -> None:
    arg_parser.add_argument(
        "--coinjoin-infrastructure-local-build",
        action="store_true",
        default=False,
        help="Build btc-node, JoinMarket client-server, and IRC server locally inside the emulator runtime.",
    )


def add_run_timezone_argument(arg_parser: argparse.ArgumentParser) -> None:
    arg_parser.add_argument(
        "--run-timezone",
        type=run_timezone,
        default=DEFAULT_RUN_TIMEZONE,
        metavar="IANA_ZONE",
        help=f"IANA timezone used in newly created run directory names (default: {DEFAULT_RUN_TIMEZONE}).",
    )


def add_joinmarket_detector_arguments(arg_parser: argparse.ArgumentParser) -> None:
    arg_parser.add_argument(
        "--joinmarket-detector",
        choices=("possible", "definite"),
        default=DEFAULT_JOINMARKET_DETECTOR,
        help=(
            "BlockSci JoinMarket subset detector to use when --coinjoin-type joinmarket "
            f"(default: {DEFAULT_JOINMARKET_DETECTOR})."
        ),
    )
    arg_parser.add_argument(
        "--joinmarket-min-base-fee",
        type=int,
        default=DEFAULT_JOINMARKET_MIN_BASE_FEE,
        help=f"Minimum base fee for the JoinMarket detector (default: {DEFAULT_JOINMARKET_MIN_BASE_FEE}).",
    )
    arg_parser.add_argument(
        "--joinmarket-percentage-fee",
        type=float,
        default=DEFAULT_JOINMARKET_PERCENTAGE_FEE,
        help=f"Percentage fee for the JoinMarket detector (default: {DEFAULT_JOINMARKET_PERCENTAGE_FEE}).",
    )
    arg_parser.add_argument(
        "--joinmarket-max-depth",
        type=int,
        default=DEFAULT_JOINMARKET_MAX_DEPTH,
        help=f"Maximum subset-search depth for the JoinMarket detector (default: {DEFAULT_JOINMARKET_MAX_DEPTH}).",
    )


def add_blocksci_script_argument(arg_parser: argparse.ArgumentParser) -> None:
    arg_parser.add_argument(
        "--blocksci-script",
        "--blocksciScript",
        dest="blocksci_script",
        metavar="PATH",
        help=(
            "Run a Python script after BlockSci parsing. The script receives ACTIVE_RUN_ID, "
            "BLOCKSCI_CONFIG, and BLOCKSCI_RUN_DIR in its environment and runs with the run directory as cwd."
        ),
    )


def add_pbs_arguments(arg_parser: argparse.ArgumentParser) -> None:
    """Add PBS-related arguments to a subparser.

    --analysisPbs:  run coinjoin-analysis stage through PBS
    --blocksciPbs:  run BlockSci parser/index/report stage through PBS
    """
    arg_parser.add_argument(
        "--analysisPbs",
        action="store_true",
        default=False,
        help="Submit coinjoin-analysis as a PBS job on MetaCentrum.",
    )
    arg_parser.add_argument(
        "--blocksciPbs",
        action="store_true",
        default=False,
        help="Submit BlockSci analysis as a PBS job on MetaCentrum.",
    )
    arg_parser.add_argument(
        "--mappingsPbs",
        action="store_true",
        default=False,
        help="Submit the Wasabi mapping enumerator and Sake as one PBS job.",
    )
    arg_parser.add_argument(
        "--pbs-ncpus",
        type=int,
        default=None,
        help=(
            "Number of CPUs for the PBS job "
            f"(default: {DEFAULT_BLOCKSCI_NCPUS} for BlockSci, "
            f"{DEFAULT_COINJOIN_ANALYSIS_NCPUS} for coinjoin-analysis)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-mem",
        type=str,
        default=None,
        help=(
            "Memory for the PBS job "
            f"(default: {DEFAULT_BLOCKSCI_MEM} for BlockSci, {DEFAULT_COINJOIN_ANALYSIS_MEM} for coinjoin-analysis)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-scratch",
        type=str,
        default=None,
        help=(
            "Scratch storage for the PBS job "
            f"(default: {DEFAULT_BLOCKSCI_SCRATCH} for BlockSci, "
            f"{DEFAULT_COINJOIN_ANALYSIS_SCRATCH} for coinjoin-analysis)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-walltime",
        type=str,
        default=None,
        help=(
            "Walltime for the PBS job "
            f"(default: {DEFAULT_BLOCKSCI_WALLTIME} for BlockSci, "
            f"{DEFAULT_COINJOIN_ANALYSIS_WALLTIME} for coinjoin-analysis)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-image",
        type=str,
        default=None,
        help="Singularity image override for either PBS stage.",
    )
    arg_parser.add_argument(
        "--pbs-blocksci-image",
        type=str,
        default=None,
        help=f"Singularity image for BlockSci PBS jobs (default: {DEFAULT_PBS_BLOCKSCI_IMAGE}).",
    )
    arg_parser.add_argument(
        "--pbs-coinjoin-analysis-image",
        type=str,
        default=None,
        help=f"Singularity image for coinjoin-analysis PBS jobs (default: {DEFAULT_PBS_COINJOIN_ANALYSIS_IMAGE}).",
    )
    arg_parser.add_argument("--pbs-mappings-enumerator-image", default=DEFAULT_MAPPINGS_ENUMERATOR_IMAGE)
    arg_parser.add_argument("--pbs-sake-image", default=DEFAULT_SAKE_IMAGE)

    def non_negative_int(value: str) -> int:
        parsed = int(value)
        if parsed < 0:
            raise argparse.ArgumentTypeError("must be non-negative")
        return parsed

    def non_negative_float(value: str) -> float:
        parsed = float(value)
        if parsed < 0:
            raise argparse.ArgumentTypeError("must be non-negative")
        return parsed

    arg_parser.add_argument("--mapping-mining-fee-rate", type=non_negative_int, default=1)
    arg_parser.add_argument("--mapping-coordination-fee-rate", type=non_negative_float, default=0.003)
    arg_parser.add_argument("--mapping-max-decomposition-fee", type=non_negative_int, default=6000)
    arg_parser.add_argument("--mapping-mode", choices=("numeric", "all"), default="numeric")
    arg_parser.add_argument("--mapping-timeout", type=positive_int, default=60)
    arg_parser.add_argument("--mapping-retry-timeout", type=positive_int, default=600)
    arg_parser.add_argument("--sake-seed", type=non_negative_int, default=20260704)
    arg_parser.add_argument(
        "--pbs-bitcoin-datadir",
        type=str,
        default=os.environ.get("PBS_BITCOIN_DATADIR"),
        help=(
            "Shared /storage Bitcoin Core datadir for BlockSci PBS jobs. "
            "It must contain regtest/blocks. Can also be set with PBS_BITCOIN_DATADIR."
        ),
    )


def add_unified_report_pbs_arguments(arg_parser: argparse.ArgumentParser) -> None:
    """Add resource overrides for the S3 report-only PBS job."""
    arg_parser.add_argument(
        "--pbs-unified-report-ncpus",
        type=int,
        default=None,
        help=(
            "CPU count for the unified-report PBS job "
            f"(default: {DEFAULT_UNIFIED_REPORT_NCPUS}; overrides --pbs-ncpus)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-unified-report-mem",
        default=None,
        help=(
            "Memory for the unified-report PBS job "
            f"(default: {DEFAULT_UNIFIED_REPORT_MEM}; overrides --pbs-mem)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-unified-report-scratch",
        default=None,
        help=(
            "Scratch storage for the unified-report PBS job "
            f"(default: {DEFAULT_UNIFIED_REPORT_SCRATCH}; overrides --pbs-scratch)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-unified-report-walltime",
        default=None,
        help=(
            "Walltime for the unified-report PBS job "
            f"(default: {DEFAULT_UNIFIED_REPORT_WALLTIME}; overrides --pbs-walltime)."
        ),
    )


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").lower() not in ("", "0", "false", "no")


def resolve_pbs_image(args: argparse.Namespace, default_image: str, stage_option: str) -> str:
    stage_image = getattr(args, stage_option, None)
    if stage_image:
        return str(stage_image)
    if getattr(args, "pbs_image", None):
        return str(args.pbs_image)
    return default_image


PBSResource = TypeVar("PBSResource", int, str)


def resolve_pbs_resource(args: argparse.Namespace, name: str, default: PBSResource) -> PBSResource:
    value = getattr(args, name, None)
    return default if value is None else cast(PBSResource, value)


def resolve_unified_report_pbs_resource(
    args: argparse.Namespace,
    name: str,
    default: PBSResource,
) -> PBSResource:
    """Resolve a report-specific override before the shared PBS override."""
    report_value = getattr(args, f"pbs_unified_report_{name}", None)
    if report_value is not None:
        return cast(PBSResource, report_value)
    return resolve_pbs_resource(args, f"pbs_{name}", default)


def pbs_wait_timeout(walltime: str) -> int:
    return walltime_to_seconds(walltime) + 60 * 60


def run_blocksci_pbs_stage(
    args: argparse.Namespace,
    run_dir: Path,
    *,
    wait: bool = True,
    include_report: bool = True,
) -> None:
    """Submit BlockSci through PBS, optionally returning before completion."""
    if not args.pbs_bitcoin_datadir:
        raise PBSError("--blocksciPbs requires --pbs-bitcoin-datadir or PBS_BITCOIN_DATADIR")
    env = compose_env(
        run_dir.name,
        args.engine,
        args.coinjoin_type,
        args.min_input_count,
        args.scenario,
        args.test_values,
        args.joinmarket_detector,
        args.joinmarket_min_base_fee,
        args.joinmarket_percentage_fee,
        args.joinmarket_max_depth,
    )
    image = resolve_pbs_image(args, DEFAULT_PBS_BLOCKSCI_IMAGE, "pbs_blocksci_image")
    staged_script = stage_blocksci_script(getattr(args, "blocksci_script", None), run_dir)
    command = blocksci_pbs_command(
        run_id=run_dir.name,
        coinjoin_type=args.coinjoin_type,
        min_input_count=args.min_input_count,
        joinmarket_detector=args.joinmarket_detector,
        joinmarket_min_base_fee=args.joinmarket_min_base_fee,
        joinmarket_percentage_fee=args.joinmarket_percentage_fee,
        joinmarket_max_depth=args.joinmarket_max_depth,
        test_values=args.test_values,
        include_report=include_report,
        blocksci_script=staged_script,
    )
    walltime = resolve_pbs_resource(args, "pbs_walltime", DEFAULT_BLOCKSCI_WALLTIME)
    submit_blocksci_pbs(
        run_dir=run_dir,
        logs_root=Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve(),
        bitcoin_datadir=Path(args.pbs_bitcoin_datadir).expanduser().resolve(),
        exporters_dir=Path(env["EXPORTERS_DIR"]).expanduser().resolve(),
        image=image,
        command=command,
        ncpus=resolve_pbs_resource(args, "pbs_ncpus", DEFAULT_BLOCKSCI_NCPUS),
        mem=resolve_pbs_resource(args, "pbs_mem", DEFAULT_BLOCKSCI_MEM),
        scratch=resolve_pbs_resource(args, "pbs_scratch", DEFAULT_BLOCKSCI_SCRATCH),
        walltime=walltime,
        dry_run=args.dry_run,
    )
    if wait and not args.dry_run:
        wait_for_pbs_marker(run_dir, "blocksci", timeout_seconds=pbs_wait_timeout(walltime))


def run_coinjoin_analysis_pbs_stage(
    args: argparse.Namespace,
    run_dir: Path,
    *,
    wait: bool = True,
) -> None:
    """Submit coinjoin-analysis through PBS, optionally returning before completion."""
    analysis_action = getattr(args, "analysis_action", "collect_docker")
    baseline_path = run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json"
    if analysis_action == "analyze_only" and not baseline_path.is_file():
        raise PBSError(f"analyze_only requires an existing baseline: {baseline_path}")
    image = resolve_pbs_image(args, DEFAULT_PBS_COINJOIN_ANALYSIS_IMAGE, "pbs_coinjoin_analysis_image")
    command = coinjoin_analysis_pbs_command(analysis_action)
    walltime = resolve_pbs_resource(args, "pbs_walltime", DEFAULT_COINJOIN_ANALYSIS_WALLTIME)
    submit_coinjoin_analysis_pbs(
        run_dir=run_dir,
        output_dir=run_dir / "coinjoin-analysis_data",
        input_data_dir=run_dir / "coinjoin_emulator_data" / "data",
        image=image,
        command=command,
        ncpus=resolve_pbs_resource(args, "pbs_ncpus", DEFAULT_COINJOIN_ANALYSIS_NCPUS),
        mem=resolve_pbs_resource(args, "pbs_mem", DEFAULT_COINJOIN_ANALYSIS_MEM),
        scratch=resolve_pbs_resource(args, "pbs_scratch", DEFAULT_COINJOIN_ANALYSIS_SCRATCH),
        walltime=walltime,
        dry_run=args.dry_run,
    )
    if wait and not args.dry_run:
        wait_for_pbs_marker(run_dir, "coinjoin-analysis", timeout_seconds=pbs_wait_timeout(walltime))


def run_mappings_pbs_stage(args: argparse.Namespace, run_dir: Path) -> None:
    """Run both Wasabi mapping tools in one PBS allocation."""
    if args.engine != "wasabi" or args.coinjoin_type != "wasabi2":
        raise PBSError("CoinJoin mappings are supported only for Wasabi/wasabi2 runs")
    walltime = resolve_pbs_resource(args, "pbs_walltime", DEFAULT_COINJOIN_ANALYSIS_WALLTIME)
    submit_mappings_pbs(
        run_dir,
        args.pbs_mappings_enumerator_image,
        args.pbs_sake_image,
        mining_fee_rate=args.mapping_mining_fee_rate,
        coordination_fee_rate=args.mapping_coordination_fee_rate,
        max_decomposition_fee=args.mapping_max_decomposition_fee,
        mode=args.mapping_mode,
        timeout=args.mapping_timeout,
        retry_timeout=args.mapping_retry_timeout,
        sake_seed=args.sake_seed,
        ncpus=resolve_pbs_resource(args, "pbs_ncpus", DEFAULT_COINJOIN_ANALYSIS_NCPUS),
        mem=resolve_pbs_resource(args, "pbs_mem", DEFAULT_COINJOIN_ANALYSIS_MEM),
        scratch=resolve_pbs_resource(args, "pbs_scratch", DEFAULT_COINJOIN_ANALYSIS_SCRATCH),
        walltime=walltime,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        wait_for_pbs_marker(run_dir, "coinjoin-mappings", timeout_seconds=pbs_wait_timeout(walltime))


def run_blocksci_export_pbs_stage(args: argparse.Namespace, run_dir: Path) -> None:
    """Submit the report-only PBS job after both parallel analyzers succeed."""
    if not args.pbs_bitcoin_datadir:
        raise PBSError("--blocksciPbs requires --pbs-bitcoin-datadir or PBS_BITCOIN_DATADIR")
    env = compose_env(run_dir.name)
    command = blocksci_export_pbs_command(
        run_id=run_dir.name,
        coinjoin_type=args.coinjoin_type,
        min_input_count=args.min_input_count,
        joinmarket_detector=args.joinmarket_detector,
        joinmarket_min_base_fee=args.joinmarket_min_base_fee,
        joinmarket_percentage_fee=args.joinmarket_percentage_fee,
        joinmarket_max_depth=args.joinmarket_max_depth,
        test_values=args.test_values,
    )
    walltime = resolve_pbs_resource(args, "pbs_walltime", DEFAULT_BLOCKSCI_WALLTIME)
    submit_blocksci_pbs(
        run_dir=run_dir,
        logs_root=Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve(),
        bitcoin_datadir=Path(args.pbs_bitcoin_datadir).expanduser().resolve(),
        exporters_dir=Path(env["EXPORTERS_DIR"]).expanduser().resolve(),
        image=resolve_pbs_image(args, DEFAULT_PBS_BLOCKSCI_IMAGE, "pbs_blocksci_image"),
        command=command,
        ncpus=resolve_pbs_resource(args, "pbs_ncpus", DEFAULT_BLOCKSCI_NCPUS),
        mem=resolve_pbs_resource(args, "pbs_mem", DEFAULT_BLOCKSCI_MEM),
        scratch=resolve_pbs_resource(args, "pbs_scratch", DEFAULT_BLOCKSCI_SCRATCH),
        walltime=walltime,
        dry_run=args.dry_run,
        stage="unified-report",
        job_name="blocksci_unified_report",
    )
    if not args.dry_run:
        wait_for_pbs_marker(run_dir, "unified-report", timeout_seconds=pbs_wait_timeout(walltime))


def run_parallel_analysis(args: argparse.Namespace, run_dir: Path, logs_root: Path) -> None:
    """Launch both analyzers independently, join them, then export once."""
    failures: dict[str, BaseException] = {}
    futures: dict[concurrent.futures.Future[None], str] = {}
    pbs_futures: dict[concurrent.futures.Future[None], str] = {}

    with captured_pipeline_stage(logs_root, "Parallel analysis", run_dir):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            if getattr(args, "analysisPbs", False):
                try:
                    run_coinjoin_analysis_pbs_stage(args, run_dir, wait=False)
                    walltime = resolve_pbs_resource(args, "pbs_walltime", DEFAULT_COINJOIN_ANALYSIS_WALLTIME)
                    future = executor.submit(
                        wait_for_pbs_marker,
                        run_dir,
                        "coinjoin-analysis",
                        timeout_seconds=pbs_wait_timeout(walltime),
                    )
                    futures[future] = "coinjoin-analysis (PBS)"
                    pbs_futures[future] = "coinjoin-analysis"
                except Exception as error:
                    failures["coinjoin-analysis (PBS)"] = error
            else:
                futures[executor.submit(run_coinjoin_analysis_docker_stage, run_dir.name)] = (
                    "coinjoin-analysis (Docker)"
                )

            if getattr(args, "blocksciPbs", False):
                try:
                    run_blocksci_pbs_stage(args, run_dir, wait=False, include_report=False)
                    walltime = resolve_pbs_resource(args, "pbs_walltime", DEFAULT_BLOCKSCI_WALLTIME)
                    future = executor.submit(
                        wait_for_pbs_marker,
                        run_dir,
                        "blocksci",
                        timeout_seconds=pbs_wait_timeout(walltime),
                    )
                    futures[future] = "BlockSci (PBS)"
                    pbs_futures[future] = "blocksci"
                except Exception as error:
                    failures["BlockSci (PBS)"] = error
            else:
                futures[executor.submit(run_blocksci_docker_stage, args, run_dir, include_report=False)] = (
                    "BlockSci (Docker)"
                )

            baseline_future = next(
                (future for future, name in futures.items() if name.startswith("coinjoin-analysis")), None
            )
            if baseline_future is not None and getattr(args, "mappingsPbs", False):
                baseline_name = futures.pop(baseline_future)
                pbs_futures.pop(baseline_future, None)
                try:
                    baseline_future.result()
                    futures[executor.submit(run_mappings_pbs_stage, args, run_dir)] = "CoinJoin mappings (PBS)"
                except Exception as error:
                    failures[baseline_name] = error

            for future in concurrent.futures.as_completed(futures):
                stage_name = futures[future]
                try:
                    future.result()
                except Exception as error:
                    failures[stage_name] = error
                    for other_future, pbs_stage in pbs_futures.items():
                        if other_future is not future and not other_future.done():
                            qdel_pbs_stage(run_dir, pbs_stage)

        if failures:
            details = "; ".join(f"{stage}: {error}" for stage, error in failures.items())
            raise RuntimeError(f"Parallel analysis failed: {details}")

    with captured_pipeline_stage(logs_root, "Unified report export", run_dir):
        if getattr(args, "blocksciPbs", False):
            run_blocksci_export_pbs_stage(args, run_dir)
        else:
            args.run_dir = str(run_dir)
            run_export_only(args)


def run_serial_analysis(args: argparse.Namespace, run_dir: Path, logs_root: Path) -> None:
    """Preserve the established serial execution order and report behavior."""
    if getattr(args, "mappingsPbs", False):
        if getattr(args, "analysisPbs", False):
            with captured_pipeline_stage(logs_root, "coinjoin-analysis (PBS)", run_dir):
                run_coinjoin_analysis_pbs_stage(args, run_dir)
        else:
            run_coinjoin_analysis(run_dir.name)
        with captured_pipeline_stage(logs_root, "CoinJoin mappings (PBS)", run_dir):
            run_mappings_pbs_stage(args, run_dir)
        with captured_pipeline_stage(logs_root, "BlockSci analysis", run_dir):
            if getattr(args, "blocksciPbs", False):
                run_blocksci_pbs_stage(args, run_dir)
            else:
                run_blocksci_docker_stage(args, run_dir, include_report=True)
        return
    if getattr(args, "blocksciPbs", False):
        if getattr(args, "analysisPbs", False):
            with captured_pipeline_stage(logs_root, "coinjoin-analysis (PBS)", run_dir):
                run_coinjoin_analysis_pbs_stage(args, run_dir)
        else:
            run_coinjoin_analysis(run_dir.name)
        with captured_pipeline_stage(logs_root, "BlockSci analysis (PBS)", run_dir):
            run_blocksci_pbs_stage(args, run_dir)
        return

    if getattr(args, "analysisPbs", False):
        with captured_pipeline_stage(logs_root, "coinjoin-analysis (PBS)", run_dir):
            run_coinjoin_analysis_pbs_stage(args, run_dir)
        with captured_pipeline_stage(logs_root, "BlockSci analysis", run_dir):
            run_blocksci_docker_stage(args, run_dir, include_report=True)
        return

    staged_script = stage_blocksci_script(args.blocksci_script, run_dir)
    with captured_pipeline_stage(logs_root, "BlockSci analysis", run_dir):
        run_script(
            ANALYSIS_SCRIPT,
            active_run_id=run_dir.name,
            engine=args.engine,
            coinjoin_type=args.coinjoin_type,
            min_input_count=args.min_input_count,
            scenario=args.scenario,
            test_values=args.test_values,
            joinmarket_detector=args.joinmarket_detector,
            joinmarket_min_base_fee=args.joinmarket_min_base_fee,
            joinmarket_percentage_fee=args.joinmarket_percentage_fee,
            joinmarket_max_depth=args.joinmarket_max_depth,
            blocksci_script=staged_script,
        )


def validate_artifact_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    backend = getattr(args, "artifact_backend", "shared-storage")
    if args.action == "pbs-from-s3":
        args.artifact_backend = "s3"
        required = (
            ("artifact_uri", "--artifact-uri"),
            ("s3_endpoint_url", "--s3-endpoint-url"),
            ("run_id", "--run-id"),
            ("s3_credentials_file", "--s3-credentials-file"),
            ("s3_profile", "--s3-profile"),
        )
        for attribute, flag in required:
            if not getattr(args, attribute, None):
                parser.error(f"pbs-from-s3 requires {flag}")
        if not args.analysisPbs and not args.blocksciPbs:
            parser.error("pbs-from-s3 requires --analysisPbs or --blocksciPbs")
        report_resource_options = (
            "pbs_unified_report_ncpus",
            "pbs_unified_report_mem",
            "pbs_unified_report_scratch",
            "pbs_unified_report_walltime",
        )
        if any(getattr(args, option, None) is not None for option in report_resource_options) and not (
            args.analysisPbs and args.blocksciPbs
        ):
            parser.error(
                "unified-report PBS resource overrides require both --analysisPbs and --blocksciPbs"
            )
        if args.mappingsPbs:
            parser.error("S3-compatible mappings are not implemented yet")
    elif backend == "s3":
        if args.action == "full-run":
            if getattr(args, "driver", None) != "kubernetes":
                parser.error("full-run --artifact-backend s3 requires --driver kubernetes")
            for attribute, flag in (
                ("artifact_uri", "--artifact-uri"),
                ("s3_endpoint_url", "--s3-endpoint-url"),
                ("run_id", "--run-id"),
                ("s3_secret_name", "--s3-secret-name"),
                ("s3_credentials_file", "--s3-credentials-file"),
                ("s3_profile", "--s3-profile"),
            ):
                if not getattr(args, attribute, None):
                    parser.error(f"full-run --artifact-backend s3 requires {flag}")
            if not args.analysisPbs or not args.blocksciPbs:
                parser.error("full-run --artifact-backend s3 requires both --analysisPbs and --blocksciPbs")
            if not getattr(args, "reuse_namespace", False):
                parser.error(
                    "Kubernetes S3-compatible mode requires --reuse-namespace because "
                    "the credentials Secret must exist before the Job is created"
                )
            if args.mappingsPbs:
                parser.error("S3-compatible mappings are not implemented yet")
            if getattr(args, "parallel", False):
                parser.error(
                    "full-run --artifact-backend s3 does not support --parallel "
                    "because its analyzer jobs already run in parallel"
                )
            if getattr(args, "blocksci_script", None):
                parser.error("full-run --artifact-backend s3 does not support --blocksci-script")
        elif args.action != "recreate" or getattr(args, "driver", None) != "kubernetes":
            parser.error("--artifact-backend s3 is supported only by full-run and recreate with --driver kubernetes")
        else:
            for attribute, flag in (
                ("artifact_uri", "--artifact-uri"),
                ("s3_endpoint_url", "--s3-endpoint-url"),
                ("run_id", "--run-id"),
                ("s3_secret_name", "--s3-secret-name"),
            ):
                if not getattr(args, attribute, None):
                    parser.error(f"Kubernetes S3-compatible mode requires {flag}")
            if not getattr(args, "reuse_namespace", False):
                parser.error(
                    "Kubernetes S3-compatible mode requires --reuse-namespace because "
                    "the credentials Secret must exist before the Job is created"
                )
        if (
            getattr(args, "kubernetes_btc_datadir", None)
            or getattr(args, "pbs_bitcoin_datadir", None)
            or getattr(args, "copy_to_host", False)
        ):
            parser.error(
                "Kubernetes S3-compatible mode does not support --kubernetes-btc-datadir, "
                "--pbs-bitcoin-datadir, or --copy-to-host"
            )
    try:
        if getattr(args, "artifact_uri", None):
            args.artifact_uri = validate_artifact_uri(args.artifact_uri)
        if getattr(args, "s3_endpoint_url", None):
            args.s3_endpoint_url = validate_s3_endpoint_url(args.s3_endpoint_url)
        if getattr(args, "run_id", None):
            args.run_id = validate_run_id(args.run_id)
        if getattr(args, "s3_credentials_file", None):
            args.s3_credentials_file = validate_credentials_file(args.s3_credentials_file)
        if getattr(args, "s3_profile", None):
            args.s3_profile = validate_s3_profile(args.s3_profile)
    except ValueError as error:
        parser.error(str(error))


def run_kubernetes_s3_emulation(args: argparse.Namespace) -> None:
    env = compose_env(engine=args.engine, scenario=args.scenario, run_timezone_name=args.run_timezone)
    scenarios_dir = Path(env["SCENARIOS_DIR"]).expanduser().resolve()
    scenario_container = (
        container_scenario_path(args.scenario, scenarios_dir, args.engine)
        if args.scenario
        else default_container_scenario(args.engine)
    )
    scenario_path = host_scenario_path(scenario_container, scenarios_dir)
    if not scenario_path.is_file():
        raise RuntimeError(f"Scenario file not found: {scenario_path}")
    kubeconfig_path = Path(args.kubeconfig).expanduser().resolve() if args.kubeconfig else Path.home() / ".kube/config"
    if not args.dry_run and not kubeconfig_path.is_file():
        raise RuntimeError(f"Kubeconfig not found: {kubeconfig_path}")
    manifest = render_s3_emulation_resources(
        namespace=args.namespace,
        run_id=args.run_id,
        scenario_json=scenario_path.read_text(encoding="utf-8"),
        engine=args.engine,
        image_prefix=args.image_prefix,
        emulator_image=os.environ.get("COINJOIN_EMULATOR_IMAGE", DEFAULT_EMULATOR_IMAGE),
        uploader_image=os.environ.get("WRAPPER_IMAGE", "ghcr.io/ondrejman/coinjoin-pipeline:latest"),
        artifact_uri=args.artifact_uri,
        endpoint_url=args.s3_endpoint_url,
        secret_name=args.s3_secret_name,
        reuse_namespace=args.reuse_namespace,
    )
    if args.dry_run:
        print(f"[dry-run] Kubernetes S3-compatible resources:\n{manifest}")
        return
    apply_s3_emulation_resources(manifest, kubeconfig_path)
    print(f"[kubernetes] Submitted S3-compatible emulation job for run {args.run_id}")


def run_pbs_from_s3(
    args: argparse.Namespace,
) -> tuple[str | None, str | None, str | None]:
    common = dict(
        artifact_uri=args.artifact_uri,
        run_id=args.run_id,
        endpoint_url=args.s3_endpoint_url,
        credentials_file=args.s3_credentials_file,
        profile=args.s3_profile,
        dry_run=args.dry_run,
    )
    parallel_report = args.analysisPbs and args.blocksciPbs
    analysis_job_id = None
    blocksci_job_id = None
    if args.analysisPbs:
        analysis_job_id = submit_coinjoin_analysis_s3_pbs(
            **common,
            image=resolve_pbs_image(args, DEFAULT_PBS_COINJOIN_ANALYSIS_IMAGE, "pbs_coinjoin_analysis_image"),
            command=coinjoin_analysis_pbs_command("collect_docker"),
            ncpus=resolve_pbs_resource(args, "pbs_ncpus", DEFAULT_COINJOIN_ANALYSIS_NCPUS),
            mem=resolve_pbs_resource(args, "pbs_mem", DEFAULT_COINJOIN_ANALYSIS_MEM),
            scratch=resolve_pbs_resource(args, "pbs_scratch", DEFAULT_COINJOIN_ANALYSIS_SCRATCH),
            walltime=resolve_pbs_resource(args, "pbs_walltime", DEFAULT_COINJOIN_ANALYSIS_WALLTIME),
        )
    if args.blocksciPbs:
        blocksci_job_id = submit_blocksci_s3_pbs(
            **common,
            image=resolve_pbs_image(args, DEFAULT_PBS_BLOCKSCI_IMAGE, "pbs_blocksci_image"),
            command=blocksci_pbs_command(
                args.run_id,
                args.coinjoin_type,
                args.min_input_count,
                args.joinmarket_detector,
                args.joinmarket_min_base_fee,
                args.joinmarket_percentage_fee,
                args.joinmarket_max_depth,
                args.test_values,
                include_report=not parallel_report,
            ),
            ncpus=resolve_pbs_resource(args, "pbs_ncpus", DEFAULT_BLOCKSCI_NCPUS),
            mem=resolve_pbs_resource(args, "pbs_mem", DEFAULT_BLOCKSCI_MEM),
            scratch=resolve_pbs_resource(args, "pbs_scratch", DEFAULT_BLOCKSCI_SCRATCH),
            walltime=resolve_pbs_resource(args, "pbs_walltime", DEFAULT_BLOCKSCI_WALLTIME),
            include_report=not parallel_report,
        )
    report_job_id = None
    if parallel_report:
        dependency_job_ids = tuple(
            job_id for job_id in (analysis_job_id, blocksci_job_id) if job_id is not None
        )
        if not args.dry_run and len(dependency_job_ids) != 2:
            raise PBSError("Could not obtain both analyzer job IDs for the unified report dependency")
        report_job_id = submit_unified_report_s3_pbs(
            **common,
            image=resolve_pbs_image(args, DEFAULT_PBS_BLOCKSCI_IMAGE, "pbs_blocksci_image"),
            command=blocksci_export_pbs_command(
                args.run_id,
                args.coinjoin_type,
                args.min_input_count,
                args.joinmarket_detector,
                args.joinmarket_min_base_fee,
                args.joinmarket_percentage_fee,
                args.joinmarket_max_depth,
                args.test_values,
            ),
            ncpus=resolve_unified_report_pbs_resource(
                args, "ncpus", DEFAULT_UNIFIED_REPORT_NCPUS
            ),
            mem=resolve_unified_report_pbs_resource(
                args, "mem", DEFAULT_UNIFIED_REPORT_MEM
            ),
            scratch=resolve_unified_report_pbs_resource(
                args, "scratch", DEFAULT_UNIFIED_REPORT_SCRATCH
            ),
            walltime=resolve_unified_report_pbs_resource(
                args, "walltime", DEFAULT_UNIFIED_REPORT_WALLTIME
            ),
            dependency_job_ids=dependency_job_ids,
        )
    return analysis_job_id, blocksci_job_id, report_job_id


def run_full_run_s3(args: argparse.Namespace) -> None:
    """Orchestrate the full S3-compatible chain and wait for every stage.

    Kubernetes emulation uploads artifacts and the `.k8s/upload.done` marker,
    PBS stages download from the bucket and upload `.pbs/<stage>.done|failed`;
    this function is the only frontend-side consumer of those markers.
    """
    access = S3Access(
        endpoint_url=args.s3_endpoint_url,
        credentials_file=args.s3_credentials_file,
        profile=args.s3_profile,
    )
    run_prefix = f"{args.artifact_uri}/{args.run_id}"
    kubeconfig_path = Path(args.kubeconfig).expanduser().resolve() if args.kubeconfig else Path.home() / ".kube/config"
    job_name = s3_emulation_job_name(args.run_id)

    if args.dry_run:
        run_kubernetes_s3_emulation(args)
        print(f"[dry-run] Would wait for {run_prefix}/.k8s/upload.done (timeout {args.emulation_timeout}s)")
        run_pbs_from_s3(args)
        if args.analysisPbs:
            print(f"[dry-run] Would wait for {run_prefix}/.pbs/coinjoin-analysis.done")
        if args.blocksciPbs:
            print(f"[dry-run] Would wait for {run_prefix}/.pbs/blocksci.done")
        if args.analysisPbs and args.blocksciPbs:
            print(f"[dry-run] Would wait for {run_prefix}/.pbs/unified-report.done")
        return

    require_qsub()
    s3_access_preflight(access, args.artifact_uri)
    ensure_empty_run_prefix(access, args.artifact_uri, args.run_id)
    kubernetes_s3_auth_preflight(kubeconfig_path, args.namespace, args.reuse_namespace, args.s3_secret_name)

    run_kubernetes_s3_emulation(args)
    print(f"[full-run] Waiting for emulation upload marker {run_prefix}/.k8s/upload.done")
    try:
        wait_for_s3_marker(
            "kubernetes-emulation",
            f"{run_prefix}/.k8s/upload.done",
            f"{run_prefix}/.k8s/upload.failed",
            access,
            timeout_seconds=args.emulation_timeout,
            probe=kubernetes_job_probe(kubeconfig_path, args.namespace, job_name),
        )
    except ArtifactTransportError:
        print(collect_s3_emulation_diagnostics(kubeconfig_path, args.namespace, job_name), file=sys.stderr)
        print(
            f"[full-run] Emulation resources left in place for inspection; clean up with: "
            f"kubectl --kubeconfig {kubeconfig_path} --namespace {args.namespace} delete job {job_name}",
            file=sys.stderr,
        )
        raise

    analysis_job_id, blocksci_job_id, report_job_id = run_pbs_from_s3(args)
    analysis_walltime = resolve_pbs_resource(args, "pbs_walltime", DEFAULT_COINJOIN_ANALYSIS_WALLTIME)
    blocksci_walltime = resolve_pbs_resource(args, "pbs_walltime", DEFAULT_BLOCKSCI_WALLTIME)
    report_walltime = resolve_unified_report_pbs_resource(
        args, "walltime", DEFAULT_UNIFIED_REPORT_WALLTIME
    )
    if analysis_job_id:
        print(f"[full-run] Waiting for coinjoin-analysis marker (PBS job {analysis_job_id})")
        try:
            wait_for_s3_marker(
                "coinjoin-analysis",
                f"{run_prefix}/.pbs/coinjoin-analysis.done",
                f"{run_prefix}/.pbs/coinjoin-analysis.failed",
                access,
                timeout_seconds=pbs_wait_timeout(analysis_walltime),
                probe=pbs_job_probe(analysis_job_id),
            )
        except (ArtifactTransportError, PBSError):
            if report_job_id:
                print(
                    f"[full-run] Cancelling dependent unified-report PBS job {report_job_id}",
                    file=sys.stderr,
                )
                qdel_pbs_job(report_job_id)
            if blocksci_job_id:
                print(
                    f"[full-run] BlockSci PBS job {blocksci_job_id} is left running; "
                    f"its results still upload to the bucket (cancel with: qdel {blocksci_job_id})",
                    file=sys.stderr,
                )
            raise
    if blocksci_job_id:
        print(f"[full-run] Waiting for blocksci marker (PBS job {blocksci_job_id})")
        try:
            wait_for_s3_marker(
                "blocksci",
                f"{run_prefix}/.pbs/blocksci.done",
                f"{run_prefix}/.pbs/blocksci.failed",
                access,
                timeout_seconds=pbs_wait_timeout(blocksci_walltime),
                probe=pbs_job_probe(blocksci_job_id),
            )
        except (ArtifactTransportError, PBSError):
            if report_job_id:
                print(
                    f"[full-run] Cancelling dependent unified-report PBS job {report_job_id}",
                    file=sys.stderr,
                )
                qdel_pbs_job(report_job_id)
            raise
    if report_job_id:
        print(f"[full-run] Waiting for unified-report marker (PBS job {report_job_id})")
        wait_for_s3_marker(
            "unified-report",
            f"{run_prefix}/.pbs/unified-report.done",
            f"{run_prefix}/.pbs/unified-report.failed",
            access,
            timeout_seconds=pbs_wait_timeout(report_walltime),
            probe=pbs_job_probe(report_job_id),
        )
    print(
        f"[full-run] Completed; results under {run_prefix}/ "
        "(coinjoin-analysis_data/, blocksci_data/, coinjoinPipeline_data/, logs/)"
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the public wrapper parser for CLI and metadata consumers."""
    parser = argparse.ArgumentParser(description="Run analysis pipeline via project shell scripts.")

    add_runtime_argument(
        parser,
        default=os.environ.get(CONTAINER_RUNTIME_ENV, DEFAULT_CONTAINER_RUNTIME),
        help_text=(
            "Container runtime for host commands "
            f"(default: {DEFAULT_CONTAINER_RUNTIME}; can also be set with {CONTAINER_RUNTIME_ENV})."
        ),
    )

    subparsers = parser.add_subparsers(dest="action", required=True)

    recreate_parser = subparsers.add_parser("recreate", help="Run recreate.sh with optional JSON scenario.")
    add_runtime_argument(recreate_parser)
    add_engine_argument(recreate_parser, required=True)
    add_dry_run_argument(recreate_parser)
    recreate_parser.add_argument("--scenario", help="JSON scenario path.")
    add_run_timezone_argument(recreate_parser)
    add_kubernetes_arguments(recreate_parser)
    add_artifact_arguments(recreate_parser, kubernetes_secret=True)
    recreate_parser.add_argument(
        "--pbs-bitcoin-datadir",
        default=None,
        help="Shared-storage-only PBS Bitcoin datadir; rejected by Kubernetes S3-compatible mode.",
    )

    clean_parser = subparsers.add_parser("clean", help="Run delete.sh (remove containers + volumes).")
    add_runtime_argument(clean_parser)
    add_dry_run_argument(clean_parser)
    clean_parser.add_argument("--yes", action="store_true", help="Confirm removal of runtime containers and volumes.")
    analyze_parser = subparsers.add_parser("analyze", help="Run analysis.sh.")
    add_runtime_argument(analyze_parser)
    add_engine_argument(analyze_parser, required=True)
    add_dry_run_argument(analyze_parser)
    analyze_parser.add_argument("--run-dir", help="Emulation run folder name or path.")
    analyze_parser.add_argument("--scenario", help="Fallback scenario JSON if the run folder has no scenario.json.")
    add_coinjoin_type_argument(analyze_parser)
    analyze_parser.add_argument(
        "--min-input-count",
        type=positive_int,
        default=DEFAULT_MIN_INPUT_COUNT,
        help="Minimum transaction input count considered by detection (default: BlockSci height/test-mode threshold).",
    )
    analyze_parser.add_argument("--test-values", action="store_true", help="Use BlockSci test heuristic thresholds.")
    add_joinmarket_detector_arguments(analyze_parser)
    add_blocksci_script_argument(analyze_parser)
    add_pbs_arguments(analyze_parser)
    mappings_parser = subparsers.add_parser("mappings", help="Run Wasabi mapping enumerator and Sake via PBS.")
    add_runtime_argument(mappings_parser)
    add_engine_argument(mappings_parser, required=True)
    add_dry_run_argument(mappings_parser)
    mappings_parser.add_argument("--run-dir", required=True)
    add_coinjoin_type_argument(mappings_parser)
    add_pbs_arguments(mappings_parser)
    export_parser = subparsers.add_parser(
        "export",
        help="Run only unified_report.json export against existing analysis outputs.",
    )
    add_runtime_argument(export_parser)
    add_engine_argument(export_parser, required=True)
    add_dry_run_argument(export_parser)
    export_parser.add_argument("--run-dir", help="Emulation run folder name or path.")
    export_parser.add_argument("--scenario", help="Fallback scenario JSON if the run folder has no scenario.json.")
    add_coinjoin_type_argument(export_parser)
    export_parser.add_argument(
        "--min-input-count",
        type=positive_int,
        default=DEFAULT_MIN_INPUT_COUNT,
        help="Minimum transaction input count considered by detection (default: BlockSci height/test-mode threshold).",
    )
    export_parser.add_argument("--test-values", action="store_true", help="Use BlockSci test heuristic thresholds.")
    add_joinmarket_detector_arguments(export_parser)
    coinjoin_parser = subparsers.add_parser(
        "coinjoin-analysis",
        aliases=["coinjoin"],
        help="Run only coinjoin-analysis against one collected emulator run.",
    )
    add_runtime_argument(coinjoin_parser)
    add_dry_run_argument(coinjoin_parser)
    coinjoin_target = coinjoin_parser.add_mutually_exclusive_group()
    coinjoin_target.add_argument("--run-dir", help="Emulation run folder name or path.")
    coinjoin_target.add_argument(
        "--all-runs",
        action="store_true",
        help="Analyze every run folder under the emulation logs root.",
    )
    coinjoin_parser.add_argument(
        "--analysis-action",
        choices=("collect_docker", "analyze_only"),
        default="collect_docker",
        help=(
            "Extract emulator artifacts and analyze them (collect_docker), or rerun "
            "analysis from an existing coinjoin_tx_info.json (analyze_only)."
        ),
    )
    add_pbs_arguments(coinjoin_parser)
    initialize_parser = subparsers.add_parser(
        "initialize", help="Download all required images for recreate/analyze ahead of time."
    )
    add_runtime_argument(initialize_parser)
    add_dry_run_argument(initialize_parser)

    s3_pbs_parser = subparsers.add_parser(
        "pbs-from-s3",
        help="Submit PBS analysis for an existing CESNET/MetaCentrum S3-compatible run.",
    )
    add_runtime_argument(s3_pbs_parser)
    add_engine_argument(s3_pbs_parser, required=True)
    add_dry_run_argument(s3_pbs_parser)
    add_artifact_arguments(s3_pbs_parser, pbs_credentials=True)
    add_coinjoin_type_argument(s3_pbs_parser)
    s3_pbs_parser.add_argument("--min-input-count", type=positive_int, default=DEFAULT_MIN_INPUT_COUNT)
    s3_pbs_parser.add_argument("--test-values", action="store_true")
    add_joinmarket_detector_arguments(s3_pbs_parser)
    add_pbs_arguments(s3_pbs_parser)
    add_unified_report_pbs_arguments(s3_pbs_parser)

    full_parser = subparsers.add_parser("full-run", help="Run delete.sh, then recreate.sh, then analysis.sh.")
    add_runtime_argument(full_parser)
    add_engine_argument(full_parser, required=True)
    add_dry_run_argument(full_parser)
    full_parser.add_argument("--scenario", help="JSON scenario path.")
    add_run_timezone_argument(full_parser)
    add_coinjoin_type_argument(full_parser)
    full_parser.add_argument(
        "--min-input-count",
        type=positive_int,
        default=DEFAULT_MIN_INPUT_COUNT,
        help="Minimum transaction input count considered by detection (default: BlockSci height/test-mode threshold).",
    )
    full_parser.add_argument("--test-values", action="store_true", help="Use BlockSci test heuristic thresholds.")
    add_joinmarket_detector_arguments(full_parser)
    add_blocksci_script_argument(full_parser)
    add_kubernetes_arguments(full_parser)
    add_pbs_arguments(full_parser)
    add_unified_report_pbs_arguments(full_parser)
    add_artifact_arguments(full_parser, pbs_credentials=True, kubernetes_secret=True)
    full_parser.add_argument(
        "--parallel",
        action="store_true",
        default=False,
        help="Run BlockSci and coinjoin-analysis concurrently after emulation.",
    )
    full_parser.add_argument(
        "--emulation-timeout",
        type=positive_int,
        default=21600,
        help="Seconds to wait for the Kubernetes S3-compatible emulation upload marker (S3 backend only).",
    )

    return parser


def main() -> None:
    parser = build_parser()

    args = parser.parse_args(normalize_argv(sys.argv[1:]))
    validate_artifact_arguments(parser, args)
    if getattr(args, "blocksci_script", None):
        script_path = Path(args.blocksci_script).expanduser().resolve()
        if not script_path.is_file():
            parser.error(f"BlockSci script does not exist or is not a file: {script_path}")
        args.blocksci_script = str(script_path)
    if args.action == "clean" and not args.dry_run and not args.yes:
        parser.error("clean is destructive; pass --yes or use --dry-run")
    direct_kubernetes_pbs = (
        args.action == "full-run"
        and getattr(args, "driver", DEFAULT_DRIVER) == "kubernetes"
        and getattr(args, "blocksciPbs", False)
        and not getattr(args, "copy_to_host", False)
    )
    if direct_kubernetes_pbs:
        kubernetes_datadir = getattr(args, "kubernetes_btc_datadir", None)
        pbs_datadir = getattr(args, "pbs_bitcoin_datadir", None)
        if (
            kubernetes_datadir
            and pbs_datadir
            and Path(kubernetes_datadir).expanduser().resolve() != Path(pbs_datadir).expanduser().resolve()
        ):
            parser.error(
                "direct Kubernetes storage requires --kubernetes-btc-datadir and "
                "--pbs-bitcoin-datadir to identify the same directory"
            )
    if getattr(args, "engine", None) == "joinmarket" and hasattr(args, "coinjoin_type"):
        if args.coinjoin_type == DEFAULT_COINJOIN_TYPE:
            args.coinjoin_type = "joinmarket"
    if getattr(args, "mappingsPbs", False) and getattr(args, "engine", None) != "wasabi":
        parser.error("--mappingsPbs is supported only with --engine wasabi")
    if getattr(args, "mappingsPbs", False) and args.action not in ("full-run", "mappings"):
        parser.error("--mappingsPbs is supported only by full-run and mappings")
    if getattr(args, "mappingsPbs", False) and getattr(args, "coinjoin_type", None) != "wasabi2":
        parser.error("--mappingsPbs requires --coinjoin-type wasabi2")
    if args.action == "mappings" and not getattr(args, "mappingsPbs", False):
        parser.error("mappings requires --mappingsPbs")
    os.environ[CONTAINER_RUNTIME_ENV] = args.runtime

    use_pbs_dry_run = (
        (args.action == "analyze" and getattr(args, "blocksciPbs", False))
        or (args.action in ("coinjoin-analysis", "coinjoin") and getattr(args, "analysisPbs", False))
        or (args.action == "mappings" and getattr(args, "mappingsPbs", False))
        or args.action == "pbs-from-s3"
        or (
            args.action in ("recreate", "full-run")
            and getattr(args, "artifact_backend", "shared-storage") == "s3"
        )
    )
    if args.dry_run:
        print(f"[dry-run] action: {args.action}")
        print(f"[dry-run] runtime: {args.runtime}")
        if hasattr(args, "engine"):
            print(f"[dry-run] engine: {args.engine}")
        if use_pbs_dry_run:
            if args.action == "recreate":
                print("[dry-run] Kubernetes resources will be rendered but not applied with kubectl.")
            elif args.action == "full-run":
                print("[dry-run] Kubernetes resources and PBS job scripts will be rendered but not submitted.")
            else:
                print("[dry-run] PBS job script will be rendered but not submitted with qsub.")
        else:
            print("[dry-run] No containers, files, reports, or Kubernetes resources will be created.")
            return

    logs_root = Path(compose_env().get("EMULATION_LOGS_DIR", ".")).expanduser().resolve()
    lock_path = logs_root / ".pipeline.lock"
    requested_run = getattr(args, "run_dir", None)
    if requested_run and args.action in ("analyze", "export", "coinjoin-analysis", "coinjoin", "mappings"):
        # Validate before locking: acquire_lock creates missing parents, which
        # would turn a typo'd run id into a junk directory that then looks valid.
        lock_path = run_dir_under_root(requested_run, logs_root) / ".research.lock"
    if args.action != "pbs-from-s3":
        try:
            _lock = acquire_lock(lock_path)
        except RuntimeError as error:
            print(f"[ERROR] {error}", file=sys.stderr)
            sys.exit(2)

    coinjoin_infrastructure_local_build = getattr(
        args,
        "coinjoin_infrastructure_local_build",
        False,
    ) or truthy_env("COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD")

    if coinjoin_infrastructure_local_build:
        os.environ["COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD"] = "1"
    # Helper to detect if the user requested Kubernetes mode
    use_kubernetes = getattr(args, "driver", DEFAULT_DRIVER) == "kubernetes"

    if args.action == "pbs-from-s3":
        try:
            run_pbs_from_s3(args)
        except PBSError as error:
            print(f"[ERROR] {error}", file=sys.stderr)
            sys.exit(2)
    elif args.action == "recreate":
        logs_root = Path(compose_env().get("EMULATION_LOGS_DIR", ".")).expanduser().resolve()
        if use_kubernetes and getattr(args, "artifact_backend", "shared-storage") == "s3":
            try:
                run_kubernetes_s3_emulation(args)
            except RuntimeError as error:
                print(f"[ERROR] {error}", file=sys.stderr)
                sys.exit(2)
        elif use_kubernetes:
            before = run_dirs(logs_root)
            with captured_pipeline_stage(logs_root, "Kubernetes emulation") as stage_log:
                run_kubernetes_emulation(
                    scenario=args.scenario,
                    engine=args.engine,
                    namespace=args.namespace,
                    reuse_namespace=args.reuse_namespace,
                    image_prefix=args.image_prefix,
                    kubeconfig=args.kubeconfig,
                    coinjoin_infrastructure_local_build=coinjoin_infrastructure_local_build,
                    run_timezone_name=args.run_timezone,
                    kubernetes_btc_datadir=args.kubernetes_btc_datadir,
                    copy_to_host=args.copy_to_host,
                )
            active_run = detect_active_run(logs_root, before)
            if active_run is not None:
                stage_log.relocate_to_run(active_run)
            else:
                stage_log.relocate(logs_root / "_failed")
                if pipeline_run_id_env():
                    print("[ERROR] Emulator did not produce the expected run directory.", file=sys.stderr)
                    sys.exit(2)
        else:
            with captured_pipeline_stage(logs_root, "Docker emulation") as stage_log:
                env = compose_env(engine=args.engine)
                emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
                before = run_dirs(emulation_logs_dir)
                run_script(
                    RECREATE_SCRIPT,
                    *(["--scenario", args.scenario] if args.scenario else []),
                    engine=args.engine,
                    run_timezone_name=args.run_timezone,
                )
                latest = detect_active_run(emulation_logs_dir, before)
                if latest:
                    print(f"Active run: {latest.name}")
                    stage_log.relocate_to_run(latest)
                else:
                    stage_log.relocate(logs_root / "_failed")
                    if pipeline_run_id_env():
                        print("[ERROR] Emulator did not produce the expected run directory.", file=sys.stderr)
                        sys.exit(2)
    elif args.action == "clean":
        with captured_pipeline_stage(logs_root, "Clean containers and volumes", logs_root / "_maintenance"):
            run_script(DELETE_SCRIPT)
    elif args.action == "mappings":
        env = compose_env(engine=args.engine)
        run_dir = Path(args.run_dir).expanduser()
        if not run_dir.is_absolute():
            run_dir = Path(env["EMULATION_LOGS_DIR"]) / run_dir
        try:
            with captured_pipeline_stage(logs_root, "CoinJoin mappings (PBS)", run_dir.resolve()):
                run_mappings_pbs_stage(args, run_dir.resolve())
        except PBSError as error:
            print(f"[ERROR] {error}", file=sys.stderr)
            sys.exit(2)
    elif args.action == "analyze":
        env = compose_env(
            None,
            args.engine,
            args.coinjoin_type,
            args.min_input_count,
            test_values=args.test_values,
            joinmarket_detector=args.joinmarket_detector,
            joinmarket_min_base_fee=args.joinmarket_min_base_fee,
            joinmarket_percentage_fee=args.joinmarket_percentage_fee,
            joinmarket_max_depth=args.joinmarket_max_depth,
        )
        active_run_id = resolve_run_id(args.run_dir, env)
        if not active_run_id:
            print("[ERROR] No grouped emulation run folder found.", file=sys.stderr)
            sys.exit(2)
        run_dir = (Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve() / active_run_id).resolve()
        if getattr(args, "blocksciPbs", False):
            try:
                with captured_pipeline_stage(logs_root, "BlockSci analysis (PBS)", run_dir):
                    run_blocksci_pbs_stage(args, run_dir)
            except PBSError as error:
                print(f"[ERROR] {error}", file=sys.stderr)
                sys.exit(2)
        else:
            try:
                staged_script = stage_blocksci_script(args.blocksci_script, run_dir)
            except ValueError as error:
                parser.error(str(error))
            with captured_pipeline_stage(logs_root, "BlockSci analysis", logs_root / active_run_id):
                run_script(
                    ANALYSIS_SCRIPT,
                    active_run_id=active_run_id,
                    engine=args.engine,
                    coinjoin_type=args.coinjoin_type,
                    min_input_count=args.min_input_count,
                    scenario=args.scenario,
                    test_values=args.test_values,
                    joinmarket_detector=args.joinmarket_detector,
                    joinmarket_min_base_fee=args.joinmarket_min_base_fee,
                    joinmarket_percentage_fee=args.joinmarket_percentage_fee,
                    joinmarket_max_depth=args.joinmarket_max_depth,
                    blocksci_script=staged_script,
                )
    elif args.action == "export":
        active_run_id = resolve_run_id(args.run_dir, compose_env())
        if not active_run_id:
            print("[ERROR] No grouped emulation run folder found.", file=sys.stderr)
            sys.exit(2)
        with captured_pipeline_stage(logs_root, "Unified report export", logs_root / active_run_id):
            run_export_only(args)
    elif args.action in ("coinjoin-analysis", "coinjoin"):
        if getattr(args, "analysisPbs", False):
            env = compose_env()
            active_run_id = resolve_run_id(args.run_dir, env)
            if not active_run_id:
                print("[ERROR] No grouped emulation run folder found.", file=sys.stderr)
                sys.exit(2)
            run_dir = (Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve() / active_run_id).resolve()
            try:
                with captured_pipeline_stage(logs_root, "coinjoin-analysis (PBS)", run_dir):
                    run_coinjoin_analysis_pbs_stage(args, run_dir)
            except PBSError as error:
                print(f"[ERROR] {error}", file=sys.stderr)
                sys.exit(2)
        else:
            run_coinjoin_analysis(args.run_dir, args.all_runs, args.analysis_action)
    elif args.action == "initialize":
        with captured_pipeline_stage(logs_root, "Initialize container images", logs_root / "_maintenance"):
            initialize_images()
    elif args.action == "full-run":
        env = compose_env(
            None,
            args.engine,
            args.coinjoin_type,
            args.min_input_count,
            args.scenario,
            args.test_values,
            args.joinmarket_detector,
            args.joinmarket_min_base_fee,
            args.joinmarket_percentage_fee,
            args.joinmarket_max_depth,
        )
        emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
        if getattr(args, "artifact_backend", "shared-storage") == "s3":
            # S3 full-run: k8s emulation → S3 markers → PBS analysis chain, all in the bucket
            try:
                run_full_run_s3(args)
            except (PBSError, RuntimeError) as error:
                print(f"[ERROR] {error}", file=sys.stderr)
                sys.exit(2)
        elif use_kubernetes:
            # Kubernetes full-run: clean → k8s emulation → local analysis
            with captured_pipeline_stage(logs_root, "Clean containers and volumes", logs_root / "_maintenance"):
                run_script(DELETE_SCRIPT)
            before = run_dirs(emulation_logs_dir)
            with captured_pipeline_stage(logs_root, "Kubernetes emulation") as emulation_log:
                run_kubernetes_emulation(
                    scenario=args.scenario,
                    engine=args.engine,
                    namespace=args.namespace,
                    reuse_namespace=args.reuse_namespace,
                    image_prefix=args.image_prefix,
                    kubeconfig=args.kubeconfig,
                    coinjoin_infrastructure_local_build=coinjoin_infrastructure_local_build,
                    run_timezone_name=args.run_timezone,
                    kubernetes_btc_datadir=(args.kubernetes_btc_datadir or args.pbs_bitcoin_datadir),
                    copy_to_host=args.copy_to_host,
                    prepare_local_analysis=not getattr(args, "blocksciPbs", False),
                )
            active_run = detect_active_run(emulation_logs_dir, before)
            if active_run is None:
                emulation_log.relocate(logs_root / "_failed")
                print("[ERROR] Emulator completed without creating a run directory.", file=sys.stderr)
                sys.exit(2)
            active_run_id = active_run.name
            print(f"Active run: {active_run_id}")
            emulation_log.relocate_to_run(active_run)
            try:
                if args.parallel:
                    run_parallel_analysis(args, active_run, logs_root)
                else:
                    run_serial_analysis(args, active_run, logs_root)
            except (PBSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
                print(f"[ERROR] {error}", file=sys.stderr)
                sys.exit(2)
        else:
            with captured_pipeline_stage(logs_root, "Clean containers and volumes", logs_root / "_maintenance"):
                run_script(DELETE_SCRIPT)
            before = run_dirs(emulation_logs_dir)
            with captured_pipeline_stage(logs_root, "Docker emulation") as emulation_log:
                run_script(
                    RECREATE_SCRIPT,
                    *(["--scenario", args.scenario] if args.scenario else []),
                    engine=args.engine,
                    run_timezone_name=args.run_timezone,
                )
            active_run = detect_active_run(emulation_logs_dir, before)
            if active_run is None:
                emulation_log.relocate(logs_root / "_failed")
                print("[ERROR] Emulator completed without creating a run directory.", file=sys.stderr)
                sys.exit(2)
            active_run_id = active_run.name
            print(f"Active run: {active_run_id}")
            emulation_log.relocate_to_run(active_run)
            try:
                if args.parallel:
                    run_parallel_analysis(args, active_run, logs_root)
                else:
                    run_serial_analysis(args, active_run, logs_root)
            except (PBSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
                print(f"[ERROR] {error}", file=sys.stderr)
                sys.exit(2)


if __name__ == "__main__":
    main()
