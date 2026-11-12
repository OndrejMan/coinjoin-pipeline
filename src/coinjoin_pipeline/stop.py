"""Stop one run: delete its Kubernetes resources and cancel every recorded PBS job.

This is the cleanup half of stopping. A process that still drives the run (a
``cjp run`` waiting for markers) is not signalled; stop it first, or it reports
the cancelled stages as failures.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .console import EXIT_FAILURE, EXIT_OK, fail
from .execution.kubernetes import RUN_ID_LABEL, kubectl, kubectl_command, resolve_kubeconfig
from .execution.pbs.submission import qdel_error
from .run_records import RUN_RESOURCE_TYPES, recorded_jobs
from .storage.s3 import validate_run_id
from .watch import DEFAULT_NAMESPACE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coinjoin-pipeline stop",
        description=(
            "Delete the run's labelled Kubernetes resources and qdel every PBS job recorded in "
            "<runs-root>/<run-id>/.pbs/. Artifacts in S3 and the run directory are kept."
        ),
    )
    parser.add_argument("--run-id", required=True, help="Run to stop.")
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Kubernetes namespace of the run (default: {DEFAULT_NAMESPACE}).",
    )
    parser.add_argument("--kubeconfig", help="kubeconfig file (default: $KUBECONFIG, else ~/.kube/config).")
    parser.add_argument(
        "--search-root",
        action="append",
        type=Path,
        help="Runs root holding <run-id>/.pbs/*.jobid (repeatable; default: the runs root).",
    )
    parser.add_argument("--pbs-only", action="store_true", help="Cancel PBS jobs only; leave Kubernetes alone.")
    parser.add_argument(
        "--save-pod-logs", type=Path, help="Append the run's pod logs to this file before deleting the pods."
    )
    parser.add_argument("--json", action="store_true", help="Print one JSON object instead of text.")
    return parser


def _save_pod_logs(kubeconfig: Path, namespace: str, run_id: str, path: Path) -> str | None:
    """Preserve the current pod output; None when it was saved."""
    command = kubectl_command(
        kubeconfig,
        "logs",
        "-l",
        f"{RUN_ID_LABEL}={run_id}",
        "--all-containers=true",
        "--prefix",
        "--tail=-1",
        namespace=namespace,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "Pod logs could not be saved before stopping the run."
    return None if result.returncode == 0 else "Some pod logs could not be saved before stopping the run."


def _delete_resources(kubeconfig: Path, namespace: str, run_id: str) -> str | None:
    """Delete every labelled resource without waiting; None on success."""
    try:
        result = kubectl(
            kubeconfig,
            "delete",
            RUN_RESOURCE_TYPES,
            "-l",
            f"{RUN_ID_LABEL}={run_id}",
            "--ignore-not-found",
            "--wait=false",
            namespace=namespace,
        )
    except FileNotFoundError:
        return "kubectl is missing; the run's Kubernetes resources were not deleted."
    if result.returncode != 0:
        return (result.stderr or "").strip() or "Deleting the run's Kubernetes resources failed."
    return None


@dataclass
class StopResult:
    run_id: str
    complete: bool = True
    cancelled: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def fail(self, warning: str) -> None:
        self.warnings.append(warning)
        self.complete = False

    def as_json(self) -> dict[str, object]:
        return {"runId": self.run_id, "complete": self.complete, "cancelled": self.cancelled, "warnings": self.warnings}


def stop_run(args: argparse.Namespace, roots: list[Path]) -> StopResult:
    result = StopResult(validate_run_id(args.run_id))
    if not args.pbs_only:
        kubeconfig = resolve_kubeconfig(args.kubeconfig)
        if args.save_pod_logs is not None:
            warning = _save_pod_logs(kubeconfig, args.namespace, result.run_id, args.save_pod_logs)
            if warning:
                # The logs are evidence, not cleanup: a failed copy leaves the stop complete.
                result.warnings.append(warning)
        error = _delete_resources(kubeconfig, args.namespace, result.run_id)
        if error:
            result.fail(error)
    jobs, job_warnings = recorded_jobs([result.run_id], roots)
    for warning in job_warnings:
        result.fail(warning)
    for stage, job_id in jobs[result.run_id].items():
        error = qdel_error(job_id)
        if error:
            result.fail(f"{stage}: {error}")
        else:
            result.cancelled[stage] = job_id
    return result


def main(argv: list[str] | None = None, *, runs_root: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = args.search_root or ([runs_root] if runs_root is not None else [])
    try:
        result = stop_run(args, [root.expanduser() for root in roots])
    except ValueError as error:
        return fail(str(error))
    if args.json:
        print(json.dumps(result.as_json(), sort_keys=True))
    else:
        for stage, job_id in result.cancelled.items():
            print(f"[stop] {stage}: PBS job {job_id} cancelled or already finished")
        for warning in result.warnings:
            print(f"WARNING: {warning}")
        state = "stopped" if result.complete else "not fully stopped; see the warnings"
        print(f"[stop] {result.run_id}: {state}")
    return EXIT_OK if result.complete else EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
