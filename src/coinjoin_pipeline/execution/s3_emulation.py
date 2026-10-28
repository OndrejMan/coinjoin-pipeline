"""Kubernetes S3-emulation job submission."""

from __future__ import annotations

import os
from dataclasses import replace

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution import settings
from coinjoin_pipeline.execution.scenarios import scenario_source_path
from coinjoin_pipeline.execution.kubernetes import (
    apply_s3_emulation_resources,
    render_s3_emulation_resources,
    resolve_kubeconfig,
)
from coinjoin_pipeline.execution.pbs_settings import resolve_uploader_image
from coinjoin_pipeline.paths import SCENARIOS_ROOT
from coinjoin_pipeline.registry import pinned_reference


def pin_emulation_images(args: PipelineConfiguration) -> PipelineConfiguration:
    """Pin the emulator and uploader the Job runs, so the PBS analysis records their digests.

    Only the process that launches the emulation knows what it ran; a later
    pbs-from-s3 keeps the configured tags and the report flags their digests as missing.
    """
    images = replace(
        args.images,
        emulator=pinned_reference(args.images.emulator or settings.DEFAULT_EMULATOR_IMAGE),
        uploader=pinned_reference(resolve_uploader_image(args)),
    )
    return replace(args, images=images)


def run_s3_kubernetes_emulation(args: PipelineConfiguration) -> None:
    """Render and submit a Kubernetes job that uploads its artifacts to S3."""
    scenario_path = scenario_source_path(args.scenario, SCENARIOS_ROOT, args.engine)
    if not scenario_path.is_file():
        raise RuntimeError(f"Scenario file not found: {scenario_path}")
    kubeconfig_path = resolve_kubeconfig(args.kubernetes.kubeconfig)
    if not args.dry_run and not kubeconfig_path.is_file():
        raise RuntimeError(f"Kubeconfig not found: {kubeconfig_path}")
    manifest = render_s3_emulation_resources(
        namespace=args.kubernetes.namespace,
        run_id=required(args.run_id, "--run-id"),
        scenario_json=scenario_path.read_text(encoding="utf-8"),
        engine=args.engine,
        image_prefix=args.kubernetes.image_prefix,
        emulator_image=args.images.emulator or settings.DEFAULT_EMULATOR_IMAGE,
        uploader_image=resolve_uploader_image(args),
        artifact_uri=required(args.artifacts.uri, "--artifact-uri"),
        endpoint_url=required(args.artifacts.endpoint_url, "--s3-endpoint-url"),
        secret_name=required(args.artifacts.secret_name, "--s3-secret-name"),
        emulation_timeout_seconds=args.emulation_timeout,
        reuse_namespace=args.kubernetes.reuse_namespace,
        distributor_startup_timeout=os.environ.get("COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT"),
        btc_node_image=os.environ.get("COINJOIN_BTC_NODE_IMAGE"),
        kubernetes_image_pull_policy=os.environ.get("KUBERNETES_IMAGE_PULL_POLICY"),
        btc_node_initial_block_count=os.environ.get("COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT"),
    )
    if args.dry_run:
        print(f"[dry-run] Kubernetes S3-compatible resources:\n{manifest}")
        return
    apply_s3_emulation_resources(manifest, kubeconfig_path)
    print(f"[kubernetes] Submitted S3-compatible emulation job for run {args.run_id}")
