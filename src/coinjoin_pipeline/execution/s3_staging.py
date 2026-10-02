"""Stage the exporter checkout into an S3 run prefix."""

from __future__ import annotations

from coinjoin_pipeline.configuration import PipelineConfiguration, required
from coinjoin_pipeline.execution.kubernetes import kubernetes_s3_auth_preflight, resolve_kubeconfig
from coinjoin_pipeline.paths import EXPORTERS_ROOT
from coinjoin_pipeline.storage.s3 import (
    STAGED_EXPORTERS_COMPLETE,
    STAGED_EXPORTERS_PARTIAL,
    ArtifactTransportError,
    S3Target,
    ensure_empty_run_prefix,
    s3_access_preflight,
    staged_exporters_state,
    upload_exporters,
)


def pbs_stages_need_exporters(args: PipelineConfiguration) -> bool:
    """Return whether this S3 PBS invocation runs the exporter checkout."""
    if not args.stages.blocksci:
        return False
    task = args.blocksci.task
    return args.blocksci.workflow == "combined" or task in {
        "detect",
        "external",
        "parse",
        "update",
    }


def ensure_staged_exporters(args: PipelineConfiguration) -> None:
    """Stage the checkout's exporters into a run prefix that has none.

    The BlockSci detect and unified-report jobs download
    ``.pipeline/exporters/`` from their own run prefix. An S3 full-run stages it
    before emulation, but a standalone ``pbs-from-s3`` — a resumed detect, above
    all — can start from a prefix that has none. Only the missing case uploads:
    a prefix that already carries exporters keeps the ones its earlier stages
    actually ran with.
    """
    target = S3Target.from_args(args)
    state, missing = staged_exporters_state(target.access, target.artifact_uri, target.run_id)
    if state == STAGED_EXPORTERS_COMPLETE:
        return
    if state == STAGED_EXPORTERS_PARTIAL:
        prefix = f"{target.artifact_uri}/{target.run_id}/.pipeline/exporters/"
        raise ArtifactTransportError(
            f"run prefix {prefix} carries an exporter tree without {', '.join(missing)}; "
            "it predates the blocksci_export rename or a previous upload died halfway. "
            "Re-staging it here would mix exporter versions across the run's stages, so "
            "start a fresh --run-id (or delete the prefix and restage it deliberately)"
        )
    print(f"[stage] Run prefix has no exporters; uploading from {EXPORTERS_ROOT}")
    upload_exporters(target.access, target.artifact_uri, target.run_id, EXPORTERS_ROOT)


def stage_kubernetes_s3_run(args: PipelineConfiguration) -> None:
    """Prepare the S3 run prefix before any Kubernetes Job is created.

    Shared by ``full-run --artifact-backend s3`` and the standalone ``emulate``
    S3 branch. Both preflights run before the upload on purpose: a failed auth
    check must not leave staged exporters behind under a run id that then needs
    cleaning up.
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
    upload_exporters(access, target.artifact_uri, target.run_id, EXPORTERS_ROOT)
