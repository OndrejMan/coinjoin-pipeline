"""Kubernetes S3-emulation job submission."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class S3EmulationOperations:
    """Wrapper-resolved dependencies for the Kubernetes S3 emulation job."""

    compose_environment: Callable[..., Mapping[str, str]]
    container_scenario_path: Callable[..., str]
    default_container_scenario: Callable[[str], str]
    host_scenario_path: Callable[[str, Path], Path]
    resolve_uploader_image: Callable[[argparse.Namespace], str]
    render_resources: Callable[..., str]
    apply_resources: Callable[[str, Path], None]
    default_emulator_image: str


def run_s3_kubernetes_emulation(
    args: argparse.Namespace,
    operations: S3EmulationOperations,
) -> None:
    """Render and submit a Kubernetes job that uploads its artifacts to S3."""
    env = operations.compose_environment(
        engine=args.engine, scenario=args.scenario, run_timezone_name=args.run_timezone
    )
    scenarios_dir = Path(env["SCENARIOS_DIR"]).expanduser().resolve()
    scenario_container = (
        operations.container_scenario_path(args.scenario, scenarios_dir, args.engine)
        if args.scenario
        else operations.default_container_scenario(args.engine)
    )
    scenario_path = operations.host_scenario_path(scenario_container, scenarios_dir)
    if not scenario_path.is_file():
        raise RuntimeError(f"Scenario file not found: {scenario_path}")
    kubeconfig_path = (
        Path(args.kubeconfig).expanduser().resolve()
        if args.kubeconfig
        else Path.home() / ".kube" / "config"
    )
    if not args.dry_run and not kubeconfig_path.is_file():
        raise RuntimeError(f"Kubeconfig not found: {kubeconfig_path}")
    manifest = operations.render_resources(
        namespace=args.namespace,
        run_id=args.run_id,
        scenario_json=scenario_path.read_text(encoding="utf-8"),
        engine=args.engine,
        image_prefix=args.image_prefix,
        emulator_image=os.environ.get("COINJOIN_EMULATOR_IMAGE", operations.default_emulator_image),
        uploader_image=operations.resolve_uploader_image(args),
        artifact_uri=args.artifact_uri,
        endpoint_url=args.s3_endpoint_url,
        secret_name=args.s3_secret_name,
        emulation_timeout_seconds=args.emulation_timeout,
        reuse_namespace=args.reuse_namespace,
        distributor_startup_timeout=os.environ.get("COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT"),
        btc_node_image=os.environ.get("COINJOIN_BTC_NODE_IMAGE"),
        kubernetes_image_pull_policy=os.environ.get("KUBERNETES_IMAGE_PULL_POLICY"),
        btc_node_initial_block_count=os.environ.get("COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT"),
    )
    if args.dry_run:
        print(f"[dry-run] Kubernetes S3-compatible resources:\n{manifest}")
        return
    operations.apply_resources(manifest, kubeconfig_path)
    print(f"[kubernetes] Submitted S3-compatible emulation job for run {args.run_id}")
