"""Unified live log watcher for Kubernetes-backed CoinJoin runs."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import TextIO


DEFAULT_NAMESPACE = "coinjoin"
DEFAULT_WAIT_SECONDS = 120
VALID_COMPONENTS = {"controller", "uploader", "coordinator"}
PBS_SUBMISSION_RE = re.compile(
    r"\[pbs\] Submitted (?P<stage>[a-z0-9-]+)(?: S3-compatible)? PBS job: (?P<job_id>\S+)"
)
PBS_TERMINAL_STATES = {"C", "F", "X"}


@dataclass(frozen=True)
class PbsJob:
    stage: str
    job_id: str
    output_path: Path
    state: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coinjoin-pipeline watch",
        description="Discover Kubernetes/PBS sources and stream CI-style prefixed logs.",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Pipeline run ID used for Kubernetes and PBS source discovery.",
    )
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument(
        "--kubeconfig",
        default=os.environ.get("KUBECONFIG", str(Path.home() / ".kube/config")),
    )
    parser.add_argument(
        "--components",
        default="controller",
        help="Comma-separated Kubernetes log sources: controller,uploader,coordinator.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Stream controller, uploader, and coordinator.",
    )
    parser.add_argument(
        "--frontend-log", type=Path, help="Also follow the local full-run tee log."
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
    parser.add_argument(
        "--save", type=Path, help="Write the unified prefixed stream to this file."
    )
    parser.add_argument(
        "--tail", type=int, default=200, help="Initial lines per selected source."
    )
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT_SECONDS)
    parser.add_argument(
        "--no-follow", action="store_true", help="Print current logs and exit."
    )
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
    newest = max(
        items, key=lambda item: item.get("metadata", {}).get("creationTimestamp", "")
    )
    return newest.get("metadata", {}).get("name")


def _discover_pod(
    kubectl: list[str], selector: str, *, wait_seconds: int, description: str
) -> str:
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


def _parse_qstat(payload: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    for line in payload.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*=\s*(.*)$", line)
        if match:
            current = match.group(1)
            fields[current] = match.group(2).strip()
        elif current is not None and line[:1].isspace():
            fields[current] += line.strip()
    return fields


def _pbs_output_path(value: str) -> Path:
    # OpenPBS reports Output_Path as "submission-host:/absolute/path".
    path = value.split(":", 1)[1] if ":" in value else value
    return Path(path).expanduser()


def _pbs_job_details(stage: str, job_id: str) -> PbsJob:
    errors: list[str] = []
    for command in (["qstat", "-x", "-f", job_id], ["qstat", "-f", job_id]):
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            errors.append((result.stderr or result.stdout).strip())
            continue
        fields = _parse_qstat(result.stdout)
        output = fields.get("Output_Path")
        if not output:
            raise RuntimeError(f"PBS job {job_id} has no Output_Path")
        return PbsJob(
            stage=stage,
            job_id=job_id,
            output_path=_pbs_output_path(output),
            state=fields.get("job_state", "?"),
        )
    detail = next((error for error in errors if error), "job not found")
    raise RuntimeError(f"cannot inspect PBS job {job_id}: {detail}")


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
    position = 0
    announced_wait = False
    terminal_idle_polls = 0
    initial = True
    try:
        while not stop.is_set():
            if job.output_path.is_file():
                with job.output_path.open("r", encoding="utf-8", errors="replace") as stream:
                    if initial:
                        recent: deque[str] = deque(maxlen=tail)
                        while line := stream.readline():
                            recent.append(line)
                        for line in recent:
                            messages.put((name, line.rstrip("\n")))
                        position = stream.tell()
                        initial = False
                    else:
                        stream.seek(position)
                        emitted = False
                        for line in stream:
                            emitted = True
                            messages.put((name, line.rstrip("\n")))
                        position = stream.tell()
                        terminal_idle_polls = 0 if emitted else terminal_idle_polls + 1
                if not follow:
                    break
            elif not announced_wait:
                messages.put((name, "output file is not available yet; waiting for PBS to start"))
                announced_wait = True
                if not follow:
                    break
            elif last_state in PBS_TERMINAL_STATES:
                terminal_idle_polls += 1

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
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            processes[name] = process
            thread = threading.Thread(
                target=_reader, args=(name, process, messages), daemon=True
            )
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
        return 130
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

    failures = [
        process.returncode
        for process in processes.values()
        if process.returncode not in {0, None}
    ]
    return 5 if failures else 0


def main(
    argv: list[str] | None = None,
    *,
    runs_root: Path | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.tail < 0 or args.wait_seconds < 0:
        print("ERROR: --tail and --wait-seconds must be non-negative", file=sys.stderr)
        return 2

    watch_kubernetes = not args.pbs_only
    watch_pbs = args.pbs or args.pbs_only or bool(args.pbs_job)
    components = (
        VALID_COMPONENTS
        if args.all
        else {item.strip() for item in args.components.split(",") if item.strip()}
    )
    unknown = components - VALID_COMPONENTS
    if unknown or not components:
        print(
            f"ERROR: invalid --components value; expected a subset of {','.join(sorted(VALID_COMPONENTS))}",
            file=sys.stderr,
        )
        return 2

    follow = not args.no_follow
    sources: dict[str, list[str]] = {}
    outer_pod = None
    if watch_kubernetes:
        kubeconfig = Path(args.kubeconfig).expanduser().resolve()
        if not kubeconfig.is_file():
            print(f"ERROR: kubeconfig not found: {kubeconfig}", file=sys.stderr)
            return 2
        kubectl = _kubectl_base(kubeconfig, args.namespace)
        try:
            outer_pod = _discover_pod(
                kubectl,
                f"coinjoin.run-id={args.run_id}",
                wait_seconds=args.wait_seconds,
                description=f"outer pod for run {args.run_id}",
            )
        except (FileNotFoundError, RuntimeError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
        for component in sorted(components):
            if component in {"controller", "uploader"}:
                pod = outer_pod
                container = component
            else:
                try:
                    pod = _discover_pod(
                        kubectl,
                        "app=wasabi-coordinator",
                        wait_seconds=args.wait_seconds,
                        description="Wasabi coordinator pod",
                    )
                except RuntimeError as error:
                    print(f"ERROR: {error}", file=sys.stderr)
                    return 2
                container = None
            sources[component] = _kubernetes_log_command(
                kubectl, pod, container, tail=args.tail, follow=follow
            )

    frontend_log = args.frontend_log.expanduser().resolve() if args.frontend_log else None
    if frontend_log is None and watch_pbs:
        candidate = Path.home() / f"{args.run_id}-full-run.log"
        if candidate.is_file():
            frontend_log = candidate
    if frontend_log is not None:
        if not frontend_log.is_file():
            print(f"ERROR: frontend log not found: {frontend_log}", file=sys.stderr)
            return 2
        tail_command = ["tail", "-n", str(args.tail)]
        if follow:
            tail_command.append("-f")
        tail_command.append(str(frontend_log))
        sources["frontend"] = tail_command

    pbs_jobs: dict[str, PbsJob] = {}
    if watch_pbs:
        default_runs_root = (
            runs_root
            or Path(
                os.environ.get(
                    "EMULATION_LOGS_DIR",
                    Path.cwd() / "coinjoin-runs",
                )
            )
        ).expanduser().resolve()
        run_dir = (
            args.run_dir.expanduser().resolve()
            if args.run_dir
            else default_runs_root / args.run_id
        )
        job_ids = _job_ids_from_run_dir(run_dir)
        if frontend_log is not None:
            job_ids.update(_job_ids_from_frontend_log(frontend_log))
        try:
            job_ids.update(_parse_pbs_job_specs(args.pbs_job))
        except ValueError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
        if not job_ids:
            print(
                "ERROR: no PBS job IDs found; pass --frontend-log LOG, --run-dir DIR, "
                "or --pbs-job STAGE=JOB_ID",
                file=sys.stderr,
            )
            return 2
        try:
            pbs_jobs = {
                f"pbs:{stage}": _pbs_job_details(stage, job_id)
                for stage, job_id in sorted(job_ids.items())
            }
        except (FileNotFoundError, RuntimeError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2

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
        print(f"ERROR: required command not found: {error.filename}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
