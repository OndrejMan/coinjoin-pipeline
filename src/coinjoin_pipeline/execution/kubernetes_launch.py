"""EXPERIMENTAL: Local launch of Kubernetes emulation.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.kubernetes import (
    kubernetes_auth_preflight,
    resolve_kubeconfig,
)
from coinjoin_pipeline.execution.run_context import pipeline_run_id_env
from coinjoin_pipeline.execution.runtime import (
    container_runtime,
)

from . import containers, scenarios, settings


def run_kubernetes_emulation(
    config: PipelineConfiguration,
    *,
    local_build: bool = False,
    btc_datadir: str | None = None,
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
    scenario, engine, kubernetes = config.scenario, config.engine, config.kubernetes
    copy_to_host = kubernetes.copy_to_host
    env = containers.compose_env(config, create_directories=True)
    host_root_dir = containers.default_host_root_dir()
    emulation_logs_dir = Path(env["EMULATION_LOGS_DIR"]).expanduser().resolve()
    scenarios_dir = Path(env["SCENARIOS_DIR"]).expanduser().resolve()
    copy_to_host_dir = os.environ.get("KUBERNETES_COPY_TO_HOST_DIR")
    if copy_to_host and (not copy_to_host_dir):
        print(
            "[ERROR] --copy-to-host requires KUBERNETES_COPY_TO_HOST_DIR; the launcher must mount an explicit host-owned output directory.",
            file=sys.stderr,
        )
        sys.exit(2)
    local_btc_data_dir = Path(copy_to_host_dir or host_root_dir / "btc-data").expanduser().resolve()
    local_download_path = local_btc_data_dir / "data"
    shared_btc_data_path = Path(btc_datadir or local_download_path).expanduser().resolve()
    emulation_logs_dir.mkdir(parents=True, exist_ok=True)
    if copy_to_host:
        local_btc_data_dir.mkdir(parents=True, exist_ok=True)
    if scenario:
        container_scenario = scenarios.container_scenario_path(scenario, scenarios_dir, engine)
    else:
        container_scenario = scenarios.default_container_scenario(engine)
    kubeconfig_path = resolve_kubeconfig(kubernetes.kubeconfig)
    if not kubeconfig_path.exists():
        print(
            f"[ERROR] Kubeconfig not found at {kubeconfig_path}. Pass --kubeconfig or ensure ~/.kube/config exists.",
            file=sys.stderr,
        )
        sys.exit(2)
    kubernetes_auth_preflight(kubeconfig_path, kubernetes.namespace, kubernetes.reuse_namespace)
    emulator_cmd = kubernetes_emulator_command(
        config,
        container_scenario,
        btc_data_path="/btc-data/data" if copy_to_host else str(shared_btc_data_path),
        control_ip=os.environ.get("KUBERNETES_CONTROL_IP", settings.DEFAULT_K8S_CONTROL_IP),
        local_build=local_build,
    )
    runtime = container_runtime()
    emulator_image = os.environ.get("COINJOIN_EMULATOR_IMAGE", settings.DEFAULT_EMULATOR_IMAGE)
    storage_uid = os.environ.get("KUBERNETES_STORAGE_UID", str(os.getuid()))
    storage_gid = os.environ.get("KUBERNETES_STORAGE_GID", str(os.getgid()))
    emulator_network = os.environ.get("KUBERNETES_EMULATOR_CONTAINER_NETWORK", "").strip()
    kubernetes_image_pull_policy = os.environ.get("KUBERNETES_IMAGE_PULL_POLICY", "").strip()
    docker_cmd = [
        runtime,
        "run",
        "--rm",
        *containers.container_run_pull_args(emulator_image, "COINJOIN_EMULATOR_PULL_POLICY"),
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
    if kubernetes_image_pull_policy:
        docker_cmd.extend(["-e", f"KUBERNETES_IMAGE_PULL_POLICY={kubernetes_image_pull_policy}"])
    if copy_to_host:
        docker_cmd.extend(["-v", f"{local_btc_data_dir}:/btc-data:rw"])
    else:
        docker_cmd.extend(["-e", f"KUBERNETES_STORAGE_UID={storage_uid}"])
        docker_cmd.extend(["-e", f"KUBERNETES_STORAGE_GID={storage_gid}"])
    docker_cmd.extend([emulator_image, *emulator_cmd])
    print(f"[kubernetes] Running emulator with driver=kubernetes, namespace={kubernetes.namespace}")
    print(f"[kubernetes] Scenario: {container_scenario}")
    print(f"[kubernetes] Kubeconfig: {kubeconfig_path}")
    transfer_mode = "copy to host" if copy_to_host else "direct shared mount"
    print(f"[kubernetes] BTC data mode: {transfer_mode}")
    print(f"[kubernetes] BTC data output: {(local_download_path if copy_to_host else shared_btc_data_path)}")
    print(f"[kubernetes] Control IP: {os.environ.get('KUBERNETES_CONTROL_IP', settings.DEFAULT_K8S_CONTROL_IP)}")
    if emulator_network:
        print(f"[kubernetes] Emulator container network: {emulator_network}")
    try:
        containers.run_command(docker_cmd, env=os.environ.copy())
    except subprocess.CalledProcessError as exc:
        print(
            f"[ERROR] Kubernetes emulation failed with exit code {exc.returncode}",
            file=sys.stderr,
        )
        sys.exit(exc.returncode)
    if prepare_local_analysis:
        populate_btc_data_volume(local_download_path if copy_to_host else shared_btc_data_path)
    print("[kubernetes] Emulation complete. BTC data ready for analysis.")


def kubernetes_emulator_command(
    config: PipelineConfiguration,
    scenario: str,
    *,
    btc_data_path: str = "/btc-data/data",
    control_ip: str = settings.DEFAULT_K8S_CONTROL_IP,
    local_build: bool = False,
) -> list[str]:
    """Build the command for a local manager accessing Kubernetes via kubeconfig."""
    kubernetes = config.kubernetes
    command = [
        "python",
        "manager.py",
        "--engine",
        config.engine,
        "--driver",
        "kubernetes",
        "--run-timezone",
        config.run_timezone,
        "run",
        "--scenario",
        scenario,
        "--namespace",
        kubernetes.namespace,
        "--image-prefix",
        kubernetes.image_prefix,
        "--control-ip",
        control_ip,
        "--btc-node-arg=-blocksxor=0",
    ]
    pinned_run_id = pipeline_run_id_env()
    if pinned_run_id:
        command.extend(["--run-id", pinned_run_id])
    btc_node_image = os.environ.get("COINJOIN_BTC_NODE_IMAGE", "").strip()
    if btc_node_image:
        command.extend(["--btc-node-image", btc_node_image])
    if kubernetes.copy_to_host:
        command.extend(["--download-btc-data", btc_data_path])
    else:
        command.extend(["--btcFolder", btc_data_path])
    if local_build:
        command.append("--coinjoin-infrastructure-local-build")
    if kubernetes.reuse_namespace:
        command.append("--reuse-namespace")
    return command


def populate_btc_data_volume(btc_data_dir: Path) -> None:
    """Copy downloaded btc-data into the Docker named volume used by blocksci.

    The analysis compose services expect blockchain data in this checkout's
    Compose-managed ``btc_data`` volume. After a Kubernetes
    emulation run, the data lives in a local directory.  This helper copies
    it into the volume so that the existing analysis pipeline works
    unchanged.
    """
    volume_name = f"{containers.compose_env()[settings.COMPOSE_PROJECT_ENV]}_btc_data"
    runtime = container_runtime()
    helper_image = os.environ.get("COINJOIN_EMULATOR_IMAGE", settings.DEFAULT_EMULATOR_IMAGE)
    subprocess.run(
        [runtime, "volume", "create", volume_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[kubernetes] Populating {runtime} volume '{volume_name}' with btc-data...")
    try:
        containers.run_command(
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
            ]
        )
        print(f"[kubernetes] Volume '{volume_name}' populated successfully.")
    except subprocess.CalledProcessError as exc:
        print(f"[WARN] Could not populate btc_data volume: {exc}", file=sys.stderr)
