"""Stage the exporter checkout into a fresh S3 run prefix."""

from __future__ import annotations

from pathlib import Path

# The exporters ship to the compute node on their own, so the tree hash they
# record in the report has to come from the same implementation the frontend
# logs here — otherwise the two provenance values are not comparable.
from exporters.artifact_paths import STAGED_EXPORTERS_DIR
from exporters.common import tree_sha256

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.kubernetes import kubernetes_s3_auth_preflight, resolve_kubeconfig
from coinjoin_pipeline.paths import EXPORTERS_ROOT, REQUIRED_EXPORTERS
from coinjoin_pipeline.storage.s3 import (
    ArtifactTransportError,
    S3Access,
    S3Target,
    ensure_empty_run_prefix,
    run_s5cmd,
    s3_access_preflight,
    s3_object_exists,
)


def staged_exporters_uri(run_prefix: str) -> str:
    """The run's ``.pipeline/exporters/`` prefix that every BlockSci job downloads."""
    return f"{run_prefix}/{STAGED_EXPORTERS_DIR}/"


def ensure_local_exporters(exporters_dir: Path) -> None:
    """Reject an exporter tree that is missing any required exporter."""
    for relative in REQUIRED_EXPORTERS:
        candidate = exporters_dir / relative
        if not candidate.is_file():
            raise ArtifactTransportError(f"Required exporter is missing: {candidate}")


def upload_exporters(access: S3Access, run_prefix: str, exporters_dir: Path) -> None:
    """Stage the checkout's exporters into the run prefix before the Job starts.

    A checkout can hold the host's bytecode; a 3.14 ``__pycache__`` is useless to
    BlockSci's Python 3.8, so the sync leaves it out.
    """
    ensure_local_exporters(exporters_dir)
    # Informational provenance for "what exactly was staged": the upload takes
    # the live checkout, uncommitted changes included, and the node has no .git
    # to resolve a commit from. The unified report recomputes this hash from the
    # tree it actually ran, so the two can be compared after the fact.
    print(f"[stage] exporters tree sha256={tree_sha256(exporters_dir)}")
    destination = staged_exporters_uri(run_prefix)
    result = run_s5cmd(
        access,
        "sync",
        "--exclude",
        "*__pycache__*",
        "--exclude",
        "*.pyc",
        f"{exporters_dir}/",
        destination,
    )
    if result.returncode != 0:
        raise ArtifactTransportError(
            f"failed to upload exporters to {destination} (exit {result.returncode}): {(result.stderr or '').strip()}"
        )
    for relative in REQUIRED_EXPORTERS:
        if not s3_object_exists(access, f"{destination}{relative}"):
            raise ArtifactTransportError(f"exporter upload did not produce {destination}{relative}")


def stage_kubernetes_s3_run(args: PipelineConfiguration) -> None:
    """Prepare the S3 run prefix before any Kubernetes Job is created.

    Run by the ``emulate`` S3 branch. Both preflights run before the upload on
    purpose: a failed auth check must not leave staged exporters behind under a
    run id that then needs cleaning up.
    """
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
    print(f"[stage] Uploading exporters from {EXPORTERS_ROOT}")
    upload_exporters(access, target.run_prefix, EXPORTERS_ROOT)
