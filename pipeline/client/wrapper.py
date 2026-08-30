#!/usr/bin/env python3
# ruff: noqa: F401, I001

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Mapping, TextIO

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Explicit self-aliases preserve wrapper's historical re-export surface.
# pylint: disable=useless-import-alias,unused-import
from client.artifact_validation import (
    validate_artifact_arguments as validate_artifact_command_arguments,
)
from client.artifacts import (
    PROBE_QUEUED,
    PROBE_RUNNING,
    PROBE_UNKNOWN,
    ArtifactTransportError,
    S3Access,
    S3Target,
    clear_s3_stage_markers,
    ensure_empty_run_prefix,
    ensure_local_exporters,
    s3_access_preflight,
    s3_object_exists,
    staged_exporters_state,
    tree_sha256,
    upload_exporters,
    wait_for_s3_marker,
)
from client.artifacts import (
    STAGED_EXPORTERS_COMPLETE as STAGED_EXPORTERS_COMPLETE,
)
from client.artifacts import (
    STAGED_EXPORTERS_PARTIAL as STAGED_EXPORTERS_PARTIAL,
)
from client.cli_options import (
    DEFAULT_COINJOIN_TYPE,
    add_coinjoin_type_argument,
    add_dry_run_argument,
    add_engine_argument,
    add_runtime_argument,
)
from client.cli_defaults import (
    DEFAULT_DRIVER,
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    DEFAULT_K8S_IMAGE_PREFIX,
    DEFAULT_K8S_NAMESPACE,
    DEFAULT_MIN_INPUT_COUNT,
    DEFAULT_RUN_TIMEZONE,
    VALID_DRIVERS,
)
from client.cli_parser import (
    add_artifact_arguments,
    add_blocksci_reusable_arguments,
    add_blocksci_script_argument,
    add_emulator_infrastructure_image_arguments,
    add_joinmarket_detector_arguments,
    add_kubernetes_arguments,
    add_pbs_arguments,
    add_run_timezone_argument,
    add_unified_report_pbs_arguments,
    build_parser,
)
from client.cli_validation import (
    non_negative_float,
    non_negative_int,
    positive_int,
    run_timezone,
)
from client.cli_entrypoint import WrapperOperations, run_main
from client.kubernetes import (
    S3_JOB_START_TIMEOUT_SECONDS,
    apply_s3_emulation_resources,
    collect_s3_emulation_diagnostics,
    delete_s3_emulation_job,
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
from client.locks import (
    acquire_lock as acquire_pipeline_lock,
)
from client.locks import (
    close_locks as close_pipeline_locks,
)
from client.locks import (
    command_lock_path as resolve_command_lock_path,
)
from client.locks import (
    ensure_no_active_s3_pbs_submission as ensure_no_active_s3_pbs_graph,
)
from client.locks import (
    pbs_submit_lock_path as s3_pbs_submit_lock_path,
)

# pylint: enable=useless-import-alias,unused-import
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
    blocksci_analysis_pbs_command,
    blocksci_export_pbs_command,
    blocksci_external_report_pbs_command,
    blocksci_notebook_pbs_command,
    blocksci_parse_pbs_command,
    blocksci_pbs_command,
    blocksci_script_pbs_command,
    blocksci_update_pbs_command,
    coinjoin_analysis_pbs_command,
    pbs_job_probe,
    persist_pbs_job_id,
    qdel_pbs_job,
    qdel_pbs_stage,
    require_qsub,
    submit_blocksci_analyze_s3_pbs,
    submit_blocksci_parse_s3_pbs,
    submit_blocksci_pbs,
    submit_blocksci_s3_pbs,
    submit_blocksci_update_s3_pbs,
    submit_coinjoin_analysis_pbs,
    submit_coinjoin_analysis_s3_pbs,
    submit_mappings_pbs,
    submit_mappings_s3_pbs,
    submit_unified_report_s3_pbs,
    wait_for_pbs_marker,
)
from client.pbs import (
    DEFAULT_COINJOIN_ANALYSIS_IMAGE as DEFAULT_PBS_COINJOIN_ANALYSIS_IMAGE,
)
from client.pbs import (
    walltime_to_seconds as walltime_to_seconds,
)
from client.pbs_settings import (
    CONTAINER_LOCK_DIR as CONTAINER_LOCK_DIR,
)
from client.pbs_settings import (
    IMAGE_URI_SCHEMES as IMAGE_URI_SCHEMES,
)
from client.pbs_settings import (
    PBSResource as PBSResource,
)
from client.pbs_settings import (
    PBSResources as PBSResources,
)
from client.pbs_settings import (
    pbs_wait_timeout,
    resolve_pbs_image,
    resolve_unified_report_pbs_image,
    resolve_unified_report_pbs_resource,
    resolve_uploader_image,
    stage_pbs_resources,
    truthy_env,
    unified_report_image_reference,
)
from client.pbs_settings import (
    read_image_lock as read_image_lock,
)
from client.pbs_settings import (
    resolve_stage_pbs_resource as resolve_stage_pbs_resource,
)
from client.pbs_settings import (
    with_singularity_scheme as with_singularity_scheme,
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
from client.run_context import (
    detect_active_run as detect_created_run,
)
from client.run_context import (
    is_run_dir as is_pipeline_run_dir,
)
from client.run_context import (
    newest_run_dir as newest_pipeline_run_dir,
)
from client.run_context import (
    pipeline_run_id_env as pinned_pipeline_run_id,
)
from client.run_context import (
    resolve_run_id as resolve_pipeline_run_id,
)
from client.run_context import (
    run_dir_under_root as resolve_pipeline_run_dir,
)
from client.run_context import (
    run_dirs as pipeline_run_dirs,
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
from client.s3_emulation import (
    S3EmulationOperations,
    run_s3_kubernetes_emulation,
)
from client.s3_markers import (
    cancel_dependent_pbs_job as cancel_s3_dependent_pbs_job,
)
from client.s3_markers import (
    rollback_s3_pbs_submissions as rollback_s3_submissions,
)
from client.s3_markers import (
    wait_for_s3_pbs_marker,
)
from client.s3_staging import (
    ensure_staged_exporters as stage_s3_exporters,
)
from client.s3_staging import (
    pbs_stages_need_exporters as s3_pbs_stages_need_exporters,
)
from client.s3_staging import (
    stage_kubernetes_s3_run as stage_s3_kubernetes_run,
)
from client.s3_submission import (
    S3StageSubmissionOperations,
    S3SubmissionLifecycleOperations,
    S3SubmissionOperations,
    submit_s3_pbs_graph,
    submit_s3_pbs_stages,
)
from client.s3_workflow import (
    S3FullRunOperations,
    S3PBSJobs,
    run_s3_full_run,
)
from client.shared_storage_pbs import (
    SharedStoragePBSOperations,
    run_blocksci_export_stage,
    run_blocksci_stage,
    run_coinjoin_analysis_stage,
    run_mappings_stage,
)
from client.stage_executor import (
    execute_parallel_analysis,
    execute_serial_analysis,
)
from client.stages import StageKind
from client.workflow import (
    SharedStorageOperations,
    SharedStorageStageRunner,
    shared_storage_analysis_plan,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
CLIENT_DIR = Path(__file__).resolve().parent
EMULATE_SCRIPT = ROOT_DIR / "emulate.sh"
ANALYSIS_SCRIPT = ROOT_DIR / "analysis.sh"
DELETE_SCRIPT = ROOT_DIR / "delete.sh"
COMPOSE_FILE = ROOT_DIR / "compose.yaml"
COMPOSE_PROJECT = "blocksci-emulator"
COINJOIN_ANALYSIS_SOURCE_PATH_ENV = "COINJOIN_ANALYSIS_SOURCE_PATH"
COINJOIN_ANALYSIS_MOUNT_PATH_ENV = "COINJOIN_ANALYSIS_MOUNT_PATH"
COINJOIN_ANALYSIS_TARGET_PATH_ENV = "COINJOIN_ANALYSIS_TARGET_PATH"
COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV = "COINJOIN_ANALYSIS_INPUT_DATA_PATH"
DEFAULT_ACTION = "full-run"
WRAPPER_ACTIONS = (
    "emulate",
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
    "--pbs-blocksci-ncpus",
    "--pbs-blocksci-mem",
    "--pbs-blocksci-scratch",
    "--pbs-blocksci-walltime",
    "--pbs-analysis-ncpus",
    "--pbs-analysis-mem",
    "--pbs-analysis-scratch",
    "--pbs-analysis-walltime",
    "--pbs-mappings-ncpus",
    "--pbs-mappings-mem",
    "--pbs-mappings-scratch",
    "--pbs-mappings-walltime",
    "--pbs-image",
    "--uploader-image",
    "--unified-report-image",
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
    "--blocksci-workflow",
    "--blocksci-task",
    "--blocksci-notebook-port",
    "--blocksci-notebooks-dir",
    "--blocksci-external-bitcoin-datadir",
    "--blocksci-external-blocksci-dir",
    "--blocksci-network",
    "--blocksci-max-block",
    "--blocksci-cache-source-run-id",
)
OPTIONS_WITHOUT_VALUES = (
    "--coinjoin-infrastructure-local-build",
    "--analysisPbs",
    "--blocksciPbs",
    "--mappingsPbs",
    "--parallel",
)
DEFAULT_ENGINE = "wasabi"
DEFAULT_BLOCKSCI_IMAGE = "ghcr.io/ondrejman/blocksci-complete:latest"
DEFAULT_COINJOIN_ANALYSIS_IMAGE = "ghcr.io/ondrejman/coinjoin-analysis:latest"
CONTAINER_SCENARIOS_DIR = "/mnt/scenarios"
DEFAULT_CONTAINER_SCENARIO = "/mnt/scenarios/overactive-local.json"
DEFAULT_JOINMARKET_CONTAINER_SCENARIO = "/mnt/scenarios/defaultJoinMarket.json"
DEFAULT_EMULATOR_IMAGE = "ghcr.io/ondrejman/coinjoin-emulator:latest"
DEFAULT_K8S_CONTROL_IP = "host.docker.internal"
VALID_PULL_POLICIES = ("always", "missing", "never")
RUNS_ROOT_CONTAINER = "/runs/emulation/logs"
COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER = "/runs/emulation/selected"
RUN_MARKER_FILES = ("coinjoin_emulator_data/scenario.json",)
IMAGE_PROVENANCE_ENV = {
    "BLOCKSCI_IMAGE": ("BLOCKSCI_IMAGE_ID", "BLOCKSCI_IMAGE_DIGEST"),
    "COINJOIN_ANALYSIS_IMAGE": ("COINJOIN_ANALYSIS_IMAGE_ID", "COINJOIN_ANALYSIS_IMAGE_DIGEST"),
    "COINJOIN_EMULATOR_IMAGE": ("COINJOIN_EMULATOR_IMAGE_ID", "COINJOIN_EMULATOR_IMAGE_DIGEST"),
}


def acquire_lock(path: Path) -> object:
    """Compatibility façade for the process-reentrant advisory lock."""
    return acquire_pipeline_lock(path)


PBS_SUBMIT_LOCK_NAME = ".pbs-submit.lock"


def pbs_submit_lock_path(logs_root: Path, run_id: str) -> Path:
    """Compatibility façade for the per-run S3 PBS submit lock path."""
    return s3_pbs_submit_lock_path(logs_root, run_id)


def command_lock_path(args: argparse.Namespace, logs_root: Path) -> Path:
    """Compatibility façade for command lock-path selection."""
    return resolve_command_lock_path(
        args, logs_root, resolve_run_dir=run_dir_under_root
    )


def ensure_no_active_s3_pbs_submission(run_dir: Path) -> None:
    """Compatibility façade for S3 PBS overlap detection."""
    ensure_no_active_s3_pbs_graph(
        run_dir,
        job_probe=pbs_job_probe,
        active_states=frozenset({PROBE_QUEUED, PROBE_RUNNING, PROBE_UNKNOWN}),
    )


# Peer containers started through docker/podman compose. The removed in-image
# launcher stopped these from its own SIGINT/SIGTERM trap; running bare, the
# wrapper owns that cleanup itself.
PEER_CONTAINERS = (
    "blocksci_analyzer",
    "coinjoin_analysis",
    "emulator_manager",
    "btc_data_wiper",
    "dind_image_prefetch",
    "isolated_docker_daemon",
)

_LOCK_HANDLES: list[TextIO] = []
_HELD_LOCKS: dict[Path, TextIO] = {}
_CLEANUP_DONE = False


def cleanup_peer_containers() -> None:
    """Stop peer containers and release locks; safe to call more than once."""
    global _CLEANUP_DONE
    if _CLEANUP_DONE:
        return
    _CLEANUP_DONE = True
    runtime = os.environ.get(CONTAINER_RUNTIME_ENV, DEFAULT_CONTAINER_RUNTIME)
    if shutil.which(runtime):
        subprocess.run(
            [runtime, "stop", *PEER_CONTAINERS],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if runtime == "podman":
            subprocess.run(
                [runtime, "rm", "-f", "-i", *PEER_CONTAINERS],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
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


def compose_env_from_args(
    args: argparse.Namespace,
    active_run_id: str | None = None,
    *,
    include_scenario: bool = True,
) -> dict[str, str]:
    """Build the shared Compose environment from one parsed pipeline request."""
    return compose_env(
        active_run_id,
        args.engine,
        args.coinjoin_type,
        args.min_input_count,
        args.scenario if include_scenario else None,
        args.joinmarket_detector,
        args.joinmarket_min_base_fee,
        args.joinmarket_percentage_fee,
        args.joinmarket_max_depth,
    )


def compose_base_command(env: Mapping[str, str]) -> list[str]:
    """Return the common Compose invocation for this pipeline checkout."""
    return [
        *compose_command(env),
        "-f",
        str(COMPOSE_FILE),
        "-p",
        COMPOSE_PROJECT,
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
    return is_pipeline_run_dir(path, RUN_MARKER_FILES)


def run_dirs(emulation_logs_dir: Path) -> set[Path]:
    return pipeline_run_dirs(emulation_logs_dir, RUN_MARKER_FILES)


def newest_run_dir(emulation_logs_dir: Path) -> Path | None:
    return newest_pipeline_run_dir(emulation_logs_dir, RUN_MARKER_FILES)


def pipeline_run_id_env() -> str:
    return pinned_pipeline_run_id()


def detect_active_run(emulation_logs_dir: Path, before: set[Path]) -> Path | None:
    return detect_created_run(emulation_logs_dir, before, RUN_MARKER_FILES)


def run_dir_under_root(run_dir_arg: str, runs_root: Path) -> Path:
    return resolve_pipeline_run_dir(run_dir_arg, runs_root)


def resolve_run_id(run_dir_arg: str | None, env: dict[str, str]) -> str | None:
    return resolve_pipeline_run_id(
        run_dir_arg,
        Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve(),
        RUN_MARKER_FILES,
    )


def run_script(
    script: Path,
    *args: str,
    active_run_id: str | None = None,
    engine: str = DEFAULT_ENGINE,
    coinjoin_type: str = DEFAULT_COINJOIN_TYPE,
    min_input_count: int | None = DEFAULT_MIN_INPUT_COUNT,
    scenario: str | None = None,
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
    compose_cmd = compose_base_command(env)
    try:
        # 1. Pull the outer compose images
        run_command(
            [*compose_cmd, "--profile", "emulate", "--profile", "analysis", "pull"],
            cwd=CLIENT_DIR,
            env=env,
        )

        # 2. Run the prefetch task.
        # By removing --no-deps, Compose will automatically start 'dind' and wait
        # for it to be healthy before executing the prefetch commands.
        run_command(
            [*compose_cmd, "--profile", "emulate", "run", "--rm", "dind_image_prefetch"],
            cwd=CLIENT_DIR,
            env=env,
        )

        # 3. Cleanup transient services and the DinD daemon
        run_command(
            [*compose_cmd, "--profile", "emulate", "down"],
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
            "[ERROR] No grouped emulation run folder found. Run emulate/full-run first or pass --run-dir explicitly.",
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
    compose_cmd = compose_base_command(run_env)
    run_command(
        [*compose_cmd, "--profile", "analysis", "run", "--rm", "--no-deps", "coinjoin_analysis"],
        cwd=CLIENT_DIR,
        env=run_env,
    )


def run_blocksci_docker_stage(args: argparse.Namespace, run_dir: Path, *, include_report: bool) -> None:
    """Run only BlockSci through Compose, optionally deferring the unified report."""
    staged_script = stage_blocksci_script(getattr(args, "blocksci_script", None), run_dir)
    env = compose_env_from_args(args, run_dir.name)
    env["BLOCKSCI_SCRIPT"] = staged_script or ""
    env["BLOCKSCI_EXPORT_REPORT"] = "true" if include_report else "false"
    compose_cmd = compose_base_command(env)
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
    emulator_network = os.environ.get("KUBERNETES_EMULATOR_CONTAINER_NETWORK", "").strip()
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
    if emulator_network:
        docker_cmd.extend(["--network", emulator_network])
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
    if emulator_network:
        print(f"[kubernetes] Emulator container network: {emulator_network}")

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
    # Reuse the emulator image for the copy helper instead of pulling a separate
    # one: this only ever runs after a Kubernetes emulation, whose local manager
    # container already pulled it, and it is covered by the image preflight and
    # by --version/--emulator-image. A standalone alpine would be an unpinned,
    # unchecked extra pull on the critical path.
    helper_image = os.environ.get("COINJOIN_EMULATOR_IMAGE", DEFAULT_EMULATOR_IMAGE)

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


def exists_or_unreadable(path: Path) -> bool:
    """True when ``path`` is present, or hidden behind a directory we may not read.

    The analysis containers run as root, and ``blocksci_parser`` creates its
    ``parsed/`` directory with mode 0700. The wrapper now runs as the invoking
    user instead of as root inside its own container, so ``Path.is_file()``
    turns the resulting EACCES into a plain ``False`` and a finished BlockSci
    run reads as one that never happened. Everything that consumes these paths
    runs as root in a container, so "cannot look" must not mean "not there".
    """
    if path.is_file():
        return True
    for parent in path.parents:
        if not parent.exists():
            continue
        return not os.access(parent, os.R_OK | os.X_OK)
    return False


def blocksci_output_exists(run_dir: Path) -> bool:
    return exists_or_unreadable(blocksci_config_path(run_dir)) and exists_or_unreadable(
        blocksci_parsed_chain_path(run_dir)
    )


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
        shutil.copytree(
            exporters_dir,
            staged,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        ensure_local_exporters(staged)
    except (ArtifactTransportError, OSError) as error:
        raise PBSError(f"Failed to stage PBS exporters in {staged}: {error}") from error
    print(f"[stage] PBS exporters staged at {staged} (sha256={tree_sha256(staged)})")
    return staged


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
            "Run the full pipeline first, or run emulate/analyze before export."
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
        ("--uploader-image", env.get("COINJOIN_UPLOADER_IMAGE")),
        ("--unified-report-image", env.get("COINJOIN_UNIFIED_REPORT_IMAGE")),
    ]
    for flag, value in optional_args:
        if value:
            command.extend([flag, value])
    return " ".join(shlex.quote(part) for part in command)


def run_export_only(args: argparse.Namespace) -> None:
    env = compose_env_from_args(args)
    active_run_id = resolve_run_id(args.run_dir, env)
    if not active_run_id:
        print(
            "[ERROR] No emulation run folder found. Run emulate/full-run first, or pass --run-dir explicitly.",
            file=sys.stderr,
        )
        sys.exit(2)

    env = compose_env_from_args(args, active_run_id)
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    run_dir = emulation_logs_dir / active_run_id
    coinjoin_ready = exists_or_unreadable(
        run_dir / "coinjoin-analysis_data" / "coinjoin_tx_info.json"
    )
    blocksci_ready = blocksci_output_exists(run_dir)

    error = export_preflight_error(
        coinjoin_ready,
        blocksci_ready,
        run_dir,
    )
    if error:
        print(error, file=sys.stderr)
        sys.exit(2)

    compose_cmd = compose_base_command(env)
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


def run_blocksci_pbs_stage(
    args: argparse.Namespace,
    run_dir: Path,
    *,
    wait: bool = True,
    include_report: bool = True,
) -> None:
    """Compatibility façade for the shared-storage BlockSci PBS stage."""
    return run_blocksci_stage(
        args,
        run_dir,
        shared_storage_pbs_operations(),
        wait=wait,
        include_report=include_report,
    )


def run_coinjoin_analysis_pbs_stage(
    args: argparse.Namespace,
    run_dir: Path,
    *,
    wait: bool = True,
) -> None:
    """Compatibility façade for the shared-storage baseline PBS stage."""
    return run_coinjoin_analysis_stage(
        args, run_dir, shared_storage_pbs_operations(), wait=wait
    )


def run_mappings_pbs_stage(
    args: argparse.Namespace,
    run_dir: Path,
    *,
    wait: bool = True,
) -> None:
    """Compatibility façade for the shared-storage mappings PBS stage."""
    return run_mappings_stage(args, run_dir, shared_storage_pbs_operations(), wait=wait)


def run_blocksci_export_pbs_stage(
    args: argparse.Namespace,
    run_dir: Path,
    *,
    wait: bool = True,
) -> None:
    """Compatibility façade for the shared-storage report-only PBS stage."""
    return run_blocksci_export_stage(
        args, run_dir, shared_storage_pbs_operations(), wait=wait
    )


def shared_storage_pbs_operations() -> SharedStoragePBSOperations:
    """Bind shared-storage PBS adapters to their historic wrapper patch points."""
    return SharedStoragePBSOperations(
        compose_environment_from_args=compose_env_from_args,
        compose_environment=compose_env,
        stage_blocksci_script=stage_blocksci_script,
        stage_exporters=stage_pbs_exporters,
        submit_blocksci=submit_blocksci_pbs,
        submit_analysis=submit_coinjoin_analysis_pbs,
        submit_mappings=submit_mappings_pbs,
        wait_for_marker=wait_for_pbs_marker,
    )


def shared_storage_operations() -> SharedStorageOperations:
    """Bind shared-storage workflow adapters to wrapper-level compatibility names."""
    return SharedStorageOperations(
        run_coinjoin_analysis=run_coinjoin_analysis,
        run_coinjoin_analysis_docker=run_coinjoin_analysis_docker_stage,
        run_coinjoin_analysis_pbs=run_coinjoin_analysis_pbs_stage,
        run_mappings_pbs=run_mappings_pbs_stage,
        run_blocksci_docker=run_blocksci_docker_stage,
        run_blocksci_pbs=run_blocksci_pbs_stage,
        wait_for_pbs_marker=wait_for_pbs_marker,
        qdel_pbs_stage=qdel_pbs_stage,
        stage_wait_timeout=lambda values, stage: pbs_wait_timeout(
            stage_pbs_resources(values, stage)["walltime"]
        ),
        stage_blocksci_script=stage_blocksci_script,
        run_script=run_script,
        analysis_script=ANALYSIS_SCRIPT,
    )


def run_parallel_analysis(args: argparse.Namespace, run_dir: Path, logs_root: Path) -> None:
    """Launch both analyzers independently, join them, then export once."""
    plan = shared_storage_analysis_plan(args, parallel=True)
    runner = SharedStorageStageRunner(
        args, run_dir, parallel=True, operations=shared_storage_operations()
    )

    with captured_pipeline_stage(logs_root, "Parallel analysis", run_dir):
        execute_parallel_analysis(plan, runner)

    with captured_pipeline_stage(logs_root, "Unified report export", run_dir):
        report = plan.of_kind(StageKind.REPORT)
        if report is not None and report.runner == "pbs":
            run_blocksci_export_pbs_stage(args, run_dir)
        else:
            args.run_dir = str(run_dir)
            run_export_only(args)


def run_serial_analysis(args: argparse.Namespace, run_dir: Path, logs_root: Path) -> None:
    """Execute the serial form of the declared shared-storage analysis plan."""
    plan = shared_storage_analysis_plan(args, parallel=False)
    runner = SharedStorageStageRunner(
        args, run_dir, parallel=False, operations=shared_storage_operations()
    )
    with captured_pipeline_stage(logs_root, "Serial analysis", run_dir):
        execute_serial_analysis(plan, runner)


def validate_artifact_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Compatibility façade for cross-option artifact validation."""
    validate_artifact_command_arguments(parser, args)


def s3_access_from_args(args: argparse.Namespace) -> S3Access:
    """Build the frontend S3 client from the validated public arguments."""
    return S3Access(
        endpoint_url=args.s3_endpoint_url,
        credentials_file=args.s3_credentials_file,
        profile=args.s3_profile,
    )


def s3_access_from_target(target: S3Target) -> S3Access:
    """Resolve frontend access from the immutable S3 run target."""
    return target.access


def run_kubernetes_s3_emulation(args: argparse.Namespace) -> None:
    """Compatibility façade for the Kubernetes S3 emulation submission."""
    return run_s3_kubernetes_emulation(
        args,
        S3EmulationOperations(
            compose_environment=compose_env,
            container_scenario_path=container_scenario_path,
            default_container_scenario=default_container_scenario,
            host_scenario_path=host_scenario_path,
            resolve_uploader_image=resolve_uploader_image,
            render_resources=render_s3_emulation_resources,
            apply_resources=apply_s3_emulation_resources,
            default_emulator_image=DEFAULT_EMULATOR_IMAGE,
        ),
    )


def cancel_dependent_pbs_job(stage_name: str, job_id: str) -> bool:
    """Cancel a dependent stage after an upstream wait failed, and say so.

    Reports the recovery command when qdel is missing or refused, so the
    operator is never told a job was cancelled when it is still queued.
    """
    return cancel_s3_dependent_pbs_job(stage_name, job_id, qdel_job=qdel_pbs_job)


def rollback_s3_pbs_submissions(
    submitted_jobs: list[tuple[str, str]],
) -> None:
    """Cancel every job obtained before an S3 PBS graph submission failed.

    Rollback is best effort: report exactly which jobs are still queued or
    running so the operator can finish the job, instead of implying the graph
    was fully withdrawn.
    """
    rollback_s3_submissions(submitted_jobs, qdel_job=qdel_pbs_job)


def run_pbs_from_s3(args: argparse.Namespace) -> S3PBSJobs:
    # The lock lives here rather than in main() so that every caller is
    # serialised: `full-run --artifact-backend s3` reaches this function
    # directly and only holds the general .pipeline.lock, which does not stop a
    # concurrent `pbs-from-s3 --run-id <same-run>` from submitting a second
    # graph for the same run.
    return submit_s3_pbs_graph(
        args,
        S3SubmissionLifecycleOperations(
            compose_environment=compose_env,
            submit_lock_path=pbs_submit_lock_path,
            acquire_lock=acquire_lock,
            ensure_no_active_submission=ensure_no_active_s3_pbs_submission,
            submit_graph=_run_pbs_from_s3,
            rollback_submissions=rollback_s3_pbs_submissions,
        ),
    )


def _run_pbs_from_s3(
    args: argparse.Namespace,
    submitted_jobs: list[tuple[str, str]],
) -> S3PBSJobs:
    return submit_s3_pbs_stages(args, submitted_jobs, s3_stage_submission_operations())


def s3_stage_submission_operations() -> S3StageSubmissionOperations:
    """Bind the S3 PBS DAG to the historical wrapper patch points.

    Only the graph's side effects are bound here; command construction, image
    and resource resolution are imported by the graph itself.
    """
    return S3StageSubmissionOperations(
        tracker_operations=S3SubmissionOperations(
            clear_stage_markers=clear_s3_stage_markers,
        ),
        s3_preflight=s3_access_preflight,
        object_exists=s3_object_exists,
        ensure_empty_prefix=ensure_empty_run_prefix,
        ensure_exporters=ensure_staged_exporters,
        submit_analysis=submit_coinjoin_analysis_s3_pbs,
        submit_mappings=submit_mappings_s3_pbs,
        submit_update=submit_blocksci_update_s3_pbs,
        submit_blocksci=submit_blocksci_s3_pbs,
        submit_parse=submit_blocksci_parse_s3_pbs,
        submit_blocksci_work=submit_blocksci_analyze_s3_pbs,
        submit_report=submit_unified_report_s3_pbs,
    )


def pbs_stages_need_exporters(args: argparse.Namespace) -> bool:
    """Report whether this invocation submits a job that runs the exporters.

    Only the BlockSci detect path executes ``.pipeline/exporters/``: the
    combined job (which always ends in an analysis export or the report) and,
    in the reusable/cached workflows, the ``detect`` work job plus the
    decoupled unified report. Baseline-only, mappings-only, parse, update,
    script and notebook stages never bind anything but an empty exporters
    directory, so staging for them would let a local exporter problem block a
    job that cannot use them anyway.
    """
    return s3_pbs_stages_need_exporters(args)


def ensure_staged_exporters(args: argparse.Namespace) -> None:
    """Stage the checkout's exporters into a run prefix that has none.

    The BlockSci detect and unified-report jobs download
    ``.pipeline/exporters/`` from their own run prefix. An S3 full-run stages it
    before emulation, but a standalone ``pbs-from-s3`` — a resumed detect, above
    all — can start from a prefix that has none. Only the missing case uploads:
    a prefix that already carries exporters keeps the ones its earlier stages
    actually ran with.
    """
    stage_s3_exporters(
        args,
        make_access=s3_access_from_args,
        exporters_state=staged_exporters_state,
        compose_environment=compose_env,
        upload_exporter_tree=upload_exporters,
    )


def stage_kubernetes_s3_run(args: argparse.Namespace, access: S3Access) -> None:
    """Prepare the S3 run prefix before any Kubernetes Job is created.

    Shared by ``full-run --artifact-backend s3`` and the standalone ``emulate``
    S3 branch, which previously skipped these checks entirely. The cluster
    preflight runs first on purpose: a failed auth check must not leave staged
    exporters behind under a run id that then needs cleaning up.
    """
    stage_s3_kubernetes_run(
        args,
        access,
        s3_preflight=s3_access_preflight,
        kubernetes_preflight=kubernetes_s3_auth_preflight,
        ensure_empty_prefix=ensure_empty_run_prefix,
        compose_environment=compose_env,
        upload_exporter_tree=upload_exporters,
    )


def wait_for_s3_pbs_stage(
    *,
    stage: str,
    job_id: str,
    run_prefix: str,
    access: S3Access,
    walltime: str,
) -> None:
    """Wait for one PBS stage using the common S3 marker/probe contract."""
    wait_for_s3_pbs_marker(
        stage=stage,
        job_id=job_id,
        run_prefix=run_prefix,
        access=access,
        walltime=walltime,
        wait_for_marker=wait_for_s3_marker,
        pbs_probe=pbs_job_probe,
        wait_timeout=pbs_wait_timeout,
    )


def run_full_run_s3(args: argparse.Namespace) -> None:
    """Run S3 orchestration while retaining wrapper-level patch targets."""
    operations = S3FullRunOperations(
        make_access=s3_access_from_args,
        require_qsub=require_qsub,
        stage_kubernetes_run=stage_kubernetes_s3_run,
        run_kubernetes_emulation=run_kubernetes_s3_emulation,
        wait_for_marker=wait_for_s3_marker,
        kubernetes_probe=kubernetes_job_probe,
        collect_kubernetes_diagnostics=collect_s3_emulation_diagnostics,
        delete_kubernetes_job=delete_s3_emulation_job,
        kubernetes_job_name=s3_emulation_job_name,
        submit_pbs=run_pbs_from_s3,
        wait_for_pbs_stage=wait_for_s3_pbs_stage,
        cancel_dependent_pbs_job=cancel_dependent_pbs_job,
        emulation_start_timeout=S3_JOB_START_TIMEOUT_SECONDS,
    )
    run_s3_full_run(args, operations)


def wrapper_operations() -> WrapperOperations:
    """Bind the executable entrypoint to wrapper's compatibility patch points."""
    return WrapperOperations(
        install_termination_handlers=install_termination_handlers,
        build_parser=build_parser,
        normalize_argv=normalize_argv,
        validate_artifact_arguments=validate_artifact_arguments,
        default_driver=DEFAULT_DRIVER,
        default_coinjoin_type=DEFAULT_COINJOIN_TYPE,
        container_runtime_env=CONTAINER_RUNTIME_ENV,
        compose_env=compose_env,
        compose_env_from_args=compose_env_from_args,
        command_lock_path=command_lock_path,
        acquire_lock=acquire_lock,
        truthy_env=truthy_env,
        run_dirs=run_dirs,
        detect_active_run=detect_active_run,
        pipeline_run_id_env=pipeline_run_id_env,
        captured_pipeline_stage=captured_pipeline_stage,
        run_script=run_script,
        emulate_script=EMULATE_SCRIPT,
        analysis_script=ANALYSIS_SCRIPT,
        delete_script=DELETE_SCRIPT,
        s3_access_from_args=s3_access_from_args,
        stage_kubernetes_s3_run=stage_kubernetes_s3_run,
        run_kubernetes_s3_emulation=run_kubernetes_s3_emulation,
        run_kubernetes_emulation=run_kubernetes_emulation,
        run_pbs_from_s3=run_pbs_from_s3,
        run_mappings_pbs_stage=run_mappings_pbs_stage,
        run_blocksci_pbs_stage=run_blocksci_pbs_stage,
        stage_blocksci_script=stage_blocksci_script,
        resolve_run_id=resolve_run_id,
        run_export_only=run_export_only,
        run_coinjoin_analysis_pbs_stage=run_coinjoin_analysis_pbs_stage,
        run_coinjoin_analysis=run_coinjoin_analysis,
        initialize_images=initialize_images,
        run_full_run_s3=run_full_run_s3,
        run_parallel_analysis=run_parallel_analysis,
        run_serial_analysis=run_serial_analysis,
    )


def main() -> None:
    """Keep the executable wrapper to parser, operation binding, and dispatch."""
    run_main(wrapper_operations())


if __name__ == "__main__":
    main()
