"""Unified live log watcher for Kubernetes-backed CoinJoin runs."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, TextIO

from .configuration import KubernetesConfiguration
from .console import EXIT_FAILURE, EXIT_INTERRUPTED, EXIT_OK, fail
from .execution.pbs.defaults import PBS_TERMINAL_STATES
from .execution.pbs.status import job_details
from .paths import default_runs_root
from .storage.s3 import RUN_ID_RULE, is_valid_run_id

# The pipeline's own default, so a run started without --namespace is found without it.
DEFAULT_NAMESPACE = KubernetesConfiguration().namespace
DEFAULT_WAIT_SECONDS = 120
ALL_COMPONENTS = {"controller", "uploader", "engine"}
VALID_COMPONENTS = ALL_COMPONENTS | {"coordinator"}
ENGINE_SELECTORS = {
    "wasabi": "app=wasabi-coordinator",
    # coinjoin-emulator labels pods with their runtime resource name, not
    # their image/component name. The distributor is the one stable
    # JoinMarket pod across scenarios; clients are dynamically named jcs-*.
    "joinmarket": "app=joinmarket-distributor",
}
OUTER_POD_SELECTOR = "app.kubernetes.io/name=coinjoin-s3"
PBS_SUBMISSION_RE = re.compile(r"\[pbs\] Submitted (?P<stage>[a-z0-9-]+)(?: S3-compatible)? PBS job: (?P<job_id>\S+)")


def _pbs_job_details(stage: str, job_id: str) -> PbsJob:
    fields = job_details(job_id)
    if fields is None:
        raise RuntimeError(f"cannot inspect PBS job {job_id}: job not found")
    output = fields.get("Output_Path")
    if not output:
        raise RuntimeError(f"PBS job {job_id} has no Output_Path")
    return PbsJob(stage, job_id, _pbs_output_path(output), fields.get("job_state", "?"))


def _run_id(value: str) -> str:
    if not is_valid_run_id(value):
        raise argparse.ArgumentTypeError(f"run ID {RUN_ID_RULE}")
    return value


@dataclass(frozen=True)
class PbsJob:
    stage: str
    job_id: str
    output_path: Path
    state: str
    log_dir: Path | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coinjoin-pipeline watch",
        description="Discover Kubernetes/PBS sources and stream CI-style prefixed logs.",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        type=_run_id,
        help="Pipeline run ID used for Kubernetes and PBS source discovery.",
    )
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Kubernetes namespace of the run (default: {DEFAULT_NAMESPACE}).",
    )
    parser.add_argument(
        "--kubeconfig",
        default=os.environ.get("KUBECONFIG", str(Path.home() / ".kube/config")),
        help="kubeconfig file (default: $KUBECONFIG, else ~/.kube/config).",
    )
    parser.add_argument(
        "--components",
        default="controller",
        help=(
            "Comma-separated Kubernetes log sources: controller,uploader,engine "
            "(coordinator is a Wasabi-only compatibility alias)."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Stream controller, uploader, and the selected run's engine service.",
    )
    parser.add_argument(
        "--engine",
        choices=tuple(ENGINE_SELECTORS),
        help="Run engine; inferred from the outer controller pod when omitted.",
    )
    parser.add_argument(
        "--frontend-log", type=Path, help="Also follow the local full-run tee log, replayed from its start."
    )
    parser.add_argument(
        "--pbs",
        action="store_true",
        help="Also discover and stream PBS job output for this run.",
    )
    parser.add_argument(
        "--pbs-only",
        action="store_true",
        help="Stream PBS output without requiring Kubernetes access.",
    )
    parser.add_argument(
        "--pbs-job",
        action="append",
        default=[],
        metavar="STAGE=JOB_ID",
        help="Explicit PBS job (repeatable); overrides an automatically discovered stage.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Run directory containing .pbs/*.jobid (auto-detected when omitted).",
    )
    parser.add_argument("--save", type=Path, help="Write the unified prefixed stream to this file.")
    parser.add_argument(
        "--tail", type=int, default=200, help="Initial lines per Kubernetes and PBS source (default: 200)."
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help=f"How long to wait for the run's pods to appear (default: {DEFAULT_WAIT_SECONDS}).",
    )
    parser.add_argument("--no-follow", action="store_true", help="Print current logs and exit.")
    return parser


def _kubectl_base(kubeconfig: Path, namespace: str) -> list[str]:
    return ["kubectl", "--kubeconfig", str(kubeconfig), "--namespace", namespace]


def _newest_pod(payload: str) -> str | None:
    try:
        items = json.loads(payload).get("items", [])
    except (json.JSONDecodeError, AttributeError):
        return None
    if not items:
        return None
    newest = max(items, key=lambda item: item.get("metadata", {}).get("creationTimestamp", ""))
    return newest.get("metadata", {}).get("name")


def _discover_pod(kubectl: list[str], selector: str, *, wait_seconds: int, description: str) -> str:
    deadline = time.monotonic() + wait_seconds
    last_error = ""
    while True:
        result = subprocess.run(
            [*kubectl, "get", "pods", "-l", selector, "-o", "json"],
            check=False,
            capture_output=True,
            text=True,
        )
        pod = _newest_pod(result.stdout) if result.returncode == 0 else None
        if pod:
            return pod
        last_error = (result.stderr or result.stdout or "no matching pod").strip()
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for {description}: {last_error}")
        time.sleep(2)


def _discover_engine(kubectl: list[str], outer_pod: str) -> str:
    result = subprocess.run(
        [*kubectl, "get", "pod", outer_pod, "-o", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "kubectl failed").strip()
        raise RuntimeError(f"Could not inspect engine for pod {outer_pod}: {detail}")
    try:
        containers = json.loads(result.stdout).get("spec", {}).get("containers", [])
    except (json.JSONDecodeError, AttributeError) as error:
        raise RuntimeError(f"Could not parse engine metadata for pod {outer_pod}") from error
    for container in containers:
        if container.get("name") != "controller":
            continue
        for item in container.get("env") or []:
            if item.get("name") == "ENGINE" and item.get("value") in ENGINE_SELECTORS:
                return str(item["value"])
    raise RuntimeError(f"Controller pod {outer_pod} does not declare a supported ENGINE")


def _kubernetes_log_command(
    kubectl: list[str], pod: str, container: str | None, *, tail: int, follow: bool
) -> list[str]:
    command = [*kubectl, "logs", f"pod/{pod}"]
    if container:
        command.extend(["-c", container])
    command.extend([f"--tail={tail}", "--timestamps=true"])
    if follow:
        command.append("--follow=true")
    return command


def _pbs_output_path(value: str) -> Path:
    # OpenPBS reports Output_Path as "submission-host:/absolute/path".
    path = value.split(":", 1)[1] if ":" in value else value
    return Path(path).expanduser()


def _job_ids_from_frontend_log(path: Path) -> dict[str, str]:
    jobs: dict[str, str] = {}
    if not path.is_file():
        return jobs
    for match in PBS_SUBMISSION_RE.finditer(path.read_text(encoding="utf-8", errors="replace")):
        jobs[match.group("stage")] = match.group("job_id")
    return jobs


def _job_ids_from_run_dir(path: Path) -> dict[str, str]:
    marker_dir = path / ".pbs"
    jobs: dict[str, str] = {}
    if not marker_dir.is_dir():
        return jobs
    for jobid_path in sorted(marker_dir.glob("*.jobid")):
        job_id = jobid_path.read_text(encoding="utf-8").strip()
        if job_id:
            jobs[jobid_path.stem] = job_id
    return jobs


def _parse_pbs_job_specs(values: list[str]) -> dict[str, str]:
    jobs: dict[str, str] = {}
    for value in values:
        stage, separator, job_id = value.partition("=")
        if not separator or not stage.strip() or not job_id.strip():
            raise ValueError(f"invalid --pbs-job {value!r}; expected STAGE=JOB_ID")
        jobs[stage.strip()] = job_id.strip()
    return jobs


def _reader(
    name: str,
    process: subprocess.Popen[str],
    messages: queue.Queue[tuple[str, str | None]],
) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        messages.put((name, line.rstrip("\n")))
    messages.put((name, None))


def _log_lines(path: Path, positions: dict[Path, int], tail: int) -> Iterator[str]:
    """Replay a source's tail once, then read only appended bytes on each poll."""
    with path.open("rb") as stream:
        # Binary offsets remain valid for parser progress written with bare CRs;
        # TextIOWrapper.tell() can return an opaque newline-decoder cookie.
        lines = (
            line
            for chunk in iter(stream.readline, b"")
            for line in chunk.decode("utf-8", errors="replace").splitlines()
        )
        if path not in positions or path.stat().st_size < positions[path]:
            yield from deque(lines, maxlen=tail)
        else:
            stream.seek(positions[path])
            yield from lines
        positions[path] = stream.tell()


def _pbs_reader(
    name: str,
    job: PbsJob,
    messages: queue.Queue[tuple[str, str | None]],
    stop: threading.Event,
    *,
    tail: int,
    follow: bool,
) -> None:
    last_state = job.state
    messages.put((name, f"job={job.job_id} state={last_state} output={job.output_path}"))
    positions: dict[Path, int] = {}
    announced_wait = False
    terminal_idle_polls = 0
    try:
        while not stop.is_set():
            emitted = False
            if job.output_path.is_file():
                for line in _log_lines(job.output_path, positions, tail):
                    emitted = True
                    messages.put((name, line))
            elif not announced_wait:
                messages.put((name, "PBS output file is not available yet; component logs are followed separately"))
                announced_wait = True

            # Mainnet workers write directly to shared storage. PBS may keep its
            # own stdout in node-local spool until the job finishes.
            if job.log_dir is not None:
                for path in sorted(job.log_dir.glob("*.log")):
                    for line in _log_lines(path, positions, tail):
                        emitted = True
                        messages.put((name, f"[{path.stem}] {line}"))
            if not follow:
                break
            try:
                refreshed = _pbs_job_details(job.stage, job.job_id)
                if refreshed.state != last_state:
                    last_state = refreshed.state
                    messages.put((name, f"state={last_state}"))
            except (FileNotFoundError, RuntimeError) as error:
                messages.put((name, f"qstat unavailable: {error}"))
                break
            terminal_idle_polls = 0 if emitted or last_state not in PBS_TERMINAL_STATES else terminal_idle_polls + 1
            if last_state in PBS_TERMINAL_STATES and terminal_idle_polls >= 2:
                break
            stop.wait(2)
    finally:
        messages.put((name, None))


def _write_line(line: str, save_file: TextIO | None) -> None:
    print(line, flush=True)
    if save_file is not None:
        save_file.write(line + "\n")
        save_file.flush()


def stream_sources(
    sources: dict[str, list[str]],
    save_path: Path | None = None,
    *,
    pbs_jobs: dict[str, PbsJob] | None = None,
    tail: int = 200,
    follow: bool = True,
) -> int:
    messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
    processes: dict[str, subprocess.Popen[str]] = {}
    threads: list[threading.Thread] = []
    stop = threading.Event()
    save_file = None
    if save_path is not None:
        save_path = save_path.expanduser().resolve()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_file = save_path.open("a", encoding="utf-8")
        print(f"[watch] saving unified output to {save_path}", flush=True)

    try:
        for name, command in sources.items():
            # Several sources stream at once; the finally block below reaps them.
            process = subprocess.Popen(  # pylint: disable=consider-using-with
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            processes[name] = process
            thread = threading.Thread(target=_reader, args=(name, process, messages), daemon=True)
            thread.start()
            threads.append(thread)

        for name, job in (pbs_jobs or {}).items():
            thread = threading.Thread(
                target=_pbs_reader,
                args=(name, job, messages, stop),
                kwargs={"tail": tail, "follow": follow},
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        active = set(processes) | set(pbs_jobs or {})
        while active:
            name, line = messages.get()
            if line is None:
                active.discard(name)
                continue
            _write_line(f"[{name}] {line}", save_file)
    except KeyboardInterrupt:
        stop.set()
        _write_line("[watch] interrupted; the remote run is still running", save_file)
        return EXIT_INTERRUPTED
    finally:
        stop.set()
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        for thread in threads:
            thread.join(timeout=1)
        if save_file is not None:
            save_file.close()

    failures = [process.returncode for process in processes.values() if process.returncode not in {0, None}]
    return EXIT_FAILURE if failures else EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    runs_root: Path | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.tail < 0 or args.wait_seconds < 0:
        return fail("--tail and --wait-seconds must be non-negative")

    watch_kubernetes = not args.pbs_only
    watch_pbs = args.pbs or args.pbs_only or bool(args.pbs_job)
    components = ALL_COMPONENTS if args.all else {item.strip() for item in args.components.split(",") if item.strip()}
    unknown = components - VALID_COMPONENTS
    if unknown or not components:
        return fail(f"invalid --components value; expected a subset of {','.join(sorted(VALID_COMPONENTS))}")

    follow = not args.no_follow
    sources: dict[str, list[str]] = {}
    outer_pod = None
    if watch_kubernetes:
        kubeconfig = Path(args.kubeconfig).expanduser().resolve()
        if not kubeconfig.is_file():
            return fail(f"kubeconfig not found: {kubeconfig}; pass --kubeconfig, or --pbs-only for PBS output alone")
        kubectl = _kubectl_base(kubeconfig, args.namespace)
        try:
            outer_pod = _discover_pod(
                kubectl,
                f"{OUTER_POD_SELECTOR},coinjoin.run-id={args.run_id}",
                wait_seconds=args.wait_seconds,
                description=f"outer pod for run {args.run_id}",
            )
        except (FileNotFoundError, RuntimeError) as error:
            return fail(str(error))
        selected_engine = args.engine
        if components & {"engine", "coordinator"}:
            if selected_engine is None:
                try:
                    selected_engine = _discover_engine(kubectl, outer_pod)
                except RuntimeError as error:
                    return fail(str(error))
            if "coordinator" in components and selected_engine != "wasabi":
                return fail("coordinator is Wasabi-specific; use --components engine for JoinMarket runs")
        for component in sorted(components):
            if component in {"controller", "uploader"}:
                pod = outer_pod
                container = component
            else:
                selector = ENGINE_SELECTORS[selected_engine]
                try:
                    pod = _discover_pod(
                        kubectl,
                        f"{selector},coinjoin.run-id={args.run_id}",
                        wait_seconds=args.wait_seconds,
                        description=f"{selected_engine} engine pod",
                    )
                except RuntimeError as error:
                    return fail(str(error))
                container = None
            sources[component] = _kubernetes_log_command(kubectl, pod, container, tail=args.tail, follow=follow)

    frontend_log = args.frontend_log.expanduser().resolve() if args.frontend_log else None
    if frontend_log is None and watch_pbs:
        candidate = Path.home() / f"{args.run_id}-full-run.log"
        if candidate.is_file():
            frontend_log = candidate
    if frontend_log is not None:
        if not frontend_log.is_file():
            return fail(f"frontend log not found: {frontend_log}")
        # The transcript is replayed whole: it is short, and its start (dry run, Kubernetes
        # setup) is what --tail would cut off first.
        tail_command = ["tail", "-n", "+1"]
        if follow:
            tail_command.append("-f")
        tail_command.append(str(frontend_log))
        sources["frontend"] = tail_command

    pbs_jobs: dict[str, PbsJob] = {}
    if watch_pbs:
        root = runs_root.expanduser().resolve() if runs_root else default_runs_root()
        run_dir = args.run_dir.expanduser().resolve() if args.run_dir else root / args.run_id
        job_ids = _job_ids_from_run_dir(run_dir)
        if frontend_log is not None:
            job_ids.update(_job_ids_from_frontend_log(frontend_log))
        try:
            job_ids.update(_parse_pbs_job_specs(args.pbs_job))
        except ValueError as error:
            return fail(str(error))
        if not job_ids:
            return fail(
                f"no PBS job IDs found in {run_dir / '.pbs'}; pass --frontend-log LOG, --run-dir DIR, "
                "or --pbs-job STAGE=JOB_ID"
            )
        # Resolve each job on its own: .jobid files are never pruned, so one
        # stale job that qstat can no longer describe must not hide every live
        # stage. A missing qstat (FileNotFoundError) is still fatal.
        skipped: list[str] = []
        try:
            for stage, job_id in sorted(job_ids.items()):
                try:
                    job = _pbs_job_details(stage, job_id)
                    if stage in {"mainnet-restore", "mainnet-producer", "mainnet-blocksci", "mainnet-checkpoint"}:
                        job = replace(job, log_dir=run_dir / "logs" / stage)
                    pbs_jobs[f"pbs:{stage}"] = job
                except RuntimeError as error:
                    skipped.append(f"{stage}={job_id}")
                    print(f"[WARN] skipping {stage}={job_id}: {error}", file=sys.stderr)
        except FileNotFoundError as error:
            return fail(str(error))
        if not pbs_jobs:
            return fail(f"none of the discovered PBS jobs could be inspected: {', '.join(skipped)}")

    print(
        f"[watch] run={args.run_id} namespace={args.namespace} outer_pod={outer_pod or '-'} "
        f"sources={','.join([*sources, *pbs_jobs])}",
        flush=True,
    )
    try:
        return stream_sources(
            sources,
            args.save,
            pbs_jobs=pbs_jobs,
            tail=args.tail,
            follow=follow,
        )
    except FileNotFoundError as error:
        return fail(f"required command not found: {error.filename}")


if __name__ == "__main__":
    raise SystemExit(main())
