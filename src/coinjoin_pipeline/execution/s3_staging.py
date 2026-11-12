"""Prepare a fresh S3 run prefix for an emulation."""

from __future__ import annotations

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.kubernetes import kubernetes_s3_auth_preflight, resolve_kubeconfig
from coinjoin_pipeline.storage.s3 import (
    S3Target,
    ensure_empty_run_prefix,
    s3_access_preflight,
)


def stage_kubernetes_s3_run(args: PipelineConfiguration) -> None:
    """Check the S3 run prefix and the Kubernetes permissions before any Job is created."""
    target = S3Target.from_args(args)
    access = target.access
    s3_access_preflight(access, target.artifact_uri)
    kubernetes_s3_auth_preflight(
        resolve_kubeconfig(args.kubernetes.kubeconfig),
        args.kubernetes.namespace,
        args.kubernetes.reuse_namespace,
        required(args.artifacts.secret_name, "--s3-secret-name"),
    )
    ensure_empty_run_prefix(access, target.artifact_uri, target.run_id)
