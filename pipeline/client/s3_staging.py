"""S3 exporter staging and Kubernetes preflight helpers.

The functions accept wrapper-resolved operations so the compatibility facade
remains the caller that tests patch while this responsibility moves out of the
large command dispatcher.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from client.artifacts import (
    STAGED_EXPORTERS_COMPLETE,
    STAGED_EXPORTERS_PARTIAL,
    ArtifactTransportError,
    S3Access,
)


def pbs_stages_need_exporters(args: argparse.Namespace) -> bool:
    """Return whether this S3 PBS invocation runs the exporter checkout."""
    if not args.blocksciPbs:
        return False
    task = getattr(args, "blocksci_task", "detect")
    if task == "update":
        return False
    return getattr(args, "blocksci_workflow", "combined") == "combined" or task in {
        "detect",
        "external",
    }


def ensure_staged_exporters(
    args: argparse.Namespace,
    *,
    make_access: Callable[[argparse.Namespace], S3Access],
    exporters_state: Callable[[S3Access, str, str], tuple[str, Sequence[str]]],
    compose_environment: Callable[[], Mapping[str, str]],
    upload_exporter_tree: Callable[[S3Access, str, str, Path], None],
) -> None:
    """Stage exporters only when the target S3 prefix has none."""
    access = make_access(args)
    state, missing = exporters_state(access, args.artifact_uri, args.run_id)
    if state == STAGED_EXPORTERS_COMPLETE:
        return
    if state == STAGED_EXPORTERS_PARTIAL:
        prefix = f"{args.artifact_uri}/{args.run_id}/.pipeline/exporters/"
        raise ArtifactTransportError(
            f"run prefix {prefix} carries an exporter tree without {', '.join(missing)}; "
            "it predates the blocksci_export rename or a previous upload died halfway. "
            "Re-staging it here would mix exporter versions across the run's stages, so "
            "start a fresh --run-id (or delete the prefix and restage it deliberately)"
        )
    exporters_dir = Path(compose_environment()["EXPORTERS_DIR"]).expanduser().resolve()
    print(f"[stage] Run prefix has no exporters; uploading from {exporters_dir}")
    upload_exporter_tree(access, args.artifact_uri, args.run_id, exporters_dir)


def stage_kubernetes_s3_run(
    args: argparse.Namespace,
    access: S3Access,
    *,
    s3_preflight: Callable[[S3Access, str], None],
    kubernetes_preflight: Callable[[Path, str, bool, str], None],
    ensure_empty_prefix: Callable[[S3Access, str, str], None],
    compose_environment: Callable[[], Mapping[str, str]],
    upload_exporter_tree: Callable[[S3Access, str, str, Path], None],
) -> None:
    """Preflight and stage one fresh S3 prefix before a Kubernetes Job exists."""
    kubeconfig_path = (
        Path(args.kubeconfig).expanduser().resolve()
        if args.kubeconfig
        else Path.home() / ".kube/config"
    )
    s3_preflight(access, args.artifact_uri)
    kubernetes_preflight(
        kubeconfig_path, args.namespace, args.reuse_namespace, args.s3_secret_name
    )
    ensure_empty_prefix(access, args.artifact_uri, args.run_id)
    exporters_dir = Path(compose_environment()["EXPORTERS_DIR"]).expanduser().resolve()
    print(f"[stage] Uploading exporters from {exporters_dir}")
    upload_exporter_tree(access, args.artifact_uri, args.run_id, exporters_dir)
