"""Stage the exporter checkout into an S3 run prefix and check what a prefix already holds."""

from __future__ import annotations

from pathlib import Path

# The exporters ship to the compute node on their own, so the tree hash they
# record in the report has to come from the same implementation the frontend
# logs here — otherwise the two provenance values are not comparable.
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
    """The run's ``.pipeline/exporters/`` prefix that every BlockSci and report job downloads."""
    return f"{run_prefix}/.pipeline/exporters/"


def ensure_local_exporters(exporters_dir: Path) -> None:
    """Reject an exporter tree that is missing any required exporter."""
    for relative in REQUIRED_EXPORTERS:
        candidate = exporters_dir / relative
        if not candidate.is_file():
            raise ArtifactTransportError(f"Required exporter is missing: {candidate}")


STAGED_EXPORTERS_COMPLETE = "complete"
STAGED_EXPORTERS_MISSING = "missing"
STAGED_EXPORTERS_PARTIAL = "partial"


def staged_exporters_state(access: S3Access, run_prefix: str) -> tuple[str, list[str]]:
    """Classify the exporter tree already staged under a run prefix.

    One entry point is not proof of a usable tree. A prefix staged before the
    ``blocksci/`` → ``blocksci_export/`` rename still carries
    ``unified_report.py``, so a resumed run would clear the frontend check and
    only fail on the compute node, which runs the renamed analysis path. The
    same holds for an upload that died halfway through.
    """
    prefix = staged_exporters_uri(run_prefix)
    missing = [relative for relative in REQUIRED_EXPORTERS if not s3_object_exists(access, f"{prefix}{relative}")]
    if not missing:
        return STAGED_EXPORTERS_COMPLETE, []
    if len(missing) == len(REQUIRED_EXPORTERS):
        return STAGED_EXPORTERS_MISSING, missing
    return STAGED_EXPORTERS_PARTIAL, missing


def upload_exporters(access: S3Access, run_prefix: str, exporters_dir: Path) -> None:
    """Stage the checkout's exporters into the run prefix before the Job starts.

    Running bare from a checkout means the tree can hold host bytecode; shipping
    a 3.14 ``__pycache__`` into BlockSci's Python 3.8 is at best noise, so the
    sync filters it out rather than snapshotting the tree somewhere first.
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
    actually ran with. A fixed state prefix serves one run after another, so it
    is refreshed every time.
    """
    target = S3Target.from_args(args)
    if target.state_prefix:
        # Every run of a fixed prefix starts its own jobs; never reuse a previous run's tree.
        print(f"[stage] Refreshing the exporters of {target.state_prefix} from {EXPORTERS_ROOT}")
        upload_exporters(target.access, target.run_prefix, EXPORTERS_ROOT)
        return
    state, missing = staged_exporters_state(target.access, target.run_prefix)
    if state == STAGED_EXPORTERS_COMPLETE:
        return
    if state == STAGED_EXPORTERS_PARTIAL:
        prefix = staged_exporters_uri(target.run_prefix)
        raise ArtifactTransportError(
            f"run prefix {prefix} carries an exporter tree without {', '.join(missing)}; "
            "it predates the blocksci_export rename or a previous upload died halfway. "
            "Re-staging it here would mix exporter versions across the run's stages, so "
            "start a fresh --run-id (or delete the prefix and restage it deliberately)"
        )
    print(f"[stage] Run prefix has no exporters; uploading from {EXPORTERS_ROOT}")
    upload_exporters(target.access, target.run_prefix, EXPORTERS_ROOT)


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
    upload_exporters(access, target.run_prefix, EXPORTERS_ROOT)
