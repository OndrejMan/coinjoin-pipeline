"""Report the scheduler, Kubernetes and S3 state of one or more runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .console import EXIT_OK, fail
from .execution.kubernetes import resolve_kubeconfig
from .run_records import emulation_states, job_state, recorded_jobs, run_markers, scheduler_states
from .storage.s3 import ArtifactTransportError, add_s3_arguments, s3_arguments_access, validate_run_id
from .watch import DEFAULT_NAMESPACE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coinjoin-pipeline status",
        description=(
            "Report each run's recorded PBS jobs with their scheduler state, its Kubernetes "
            "emulation Job and, with --markers, its S3 terminal markers. Read-only."
        ),
    )
    parser.add_argument("--run-id", action="append", required=True, help="Run to report (repeatable).")
    parser.add_argument(
        "--namespace",
        action="append",
        help=f"Kubernetes namespace to look for emulation Jobs in (repeatable; default: {DEFAULT_NAMESPACE}).",
    )
    parser.add_argument("--kubeconfig", help="kubeconfig file (default: $KUBECONFIG, else ~/.kube/config).")
    parser.add_argument(
        "--search-root",
        action="append",
        type=Path,
        help="Runs root holding <run-id>/.pbs/*.jobid (repeatable, earlier wins; default: the runs root).",
    )
    parser.add_argument("--no-kubernetes", action="store_true", help="Skip the Kubernetes emulation Job lookup.")
    parser.add_argument("--no-pbs", action="store_true", help="Skip the PBS scheduler lookup.")
    parser.add_argument(
        "--markers", action="store_true", help="Also read the upload and stage markers from the S3 run prefix."
    )
    add_s3_arguments(parser, "S3 run root of --markers (default: $ARTIFACT_URI).")
    parser.add_argument("--json", action="store_true", help="Print one JSON object instead of text.")
    return parser


def collect(args: argparse.Namespace, roots: list[Path]) -> dict[str, object]:
    run_ids = list(dict.fromkeys(args.run_id))
    jobs, warnings = recorded_jobs(run_ids, roots)
    states = None
    if not args.no_pbs:
        states = scheduler_states()
        if states is None:
            warnings.append("PBS status is unavailable.")
    emulations: dict[str, str] = {}
    if not args.no_kubernetes:
        emulations, kubernetes_warnings = emulation_states(
            resolve_kubeconfig(args.kubeconfig), dict.fromkeys(args.namespace or [DEFAULT_NAMESPACE])
        )
        warnings.extend(kubernetes_warnings)
    access = artifact_uri = None
    if args.markers:
        artifact_uri, access = s3_arguments_access(args)
    runs: dict[str, object] = {}
    for run_id in run_ids:
        entry: dict[str, object] = {"jobIds": jobs[run_id]}
        if states is not None:
            entry["pbs"] = {stage: job_state(job_id, states) for stage, job_id in jobs[run_id].items()}
        if run_id in emulations:
            entry["emulation"] = emulations[run_id]
        if access is not None and artifact_uri is not None:
            try:
                entry["markers"] = run_markers(access, artifact_uri, run_id, jobs[run_id])
            except ArtifactTransportError as error:
                warnings.append(f"{run_id}: S3 markers are unavailable: {error}")
        runs[run_id] = entry
    return {"runs": runs, "warnings": warnings}


def _print_text(report: dict) -> None:
    for run_id, entry in report["runs"].items():
        print(f"{run_id}: emulation {entry.get('emulation', '-')}")
        markers = entry.get("markers") or {}
        if markers:
            print(f"  upload marker: {markers.get('upload') or '-'}")
        for stage, job_id in entry["jobIds"].items():
            scheduler = (entry.get("pbs") or {}).get(stage, "-")
            marker = (markers.get("stages") or {}).get(stage) or "-"
            print(f"  {stage}: {job_id} scheduler={scheduler} marker={marker}")
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")


def main(argv: list[str] | None = None, *, runs_root: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        for run_id in args.run_id:
            validate_run_id(run_id)
        roots = args.search_root or ([runs_root] if runs_root is not None else [])
        report = collect(args, [root.expanduser() for root in roots])
    except ValueError as error:
        return fail(str(error))
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        _print_text(report)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
