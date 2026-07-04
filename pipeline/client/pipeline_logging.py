"""Pipeline progress display and immutable per-stage evidence logs."""

from __future__ import annotations

import contextlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, TextIO

STAGE_SEPARATOR_WIDTH = 88


class TerminalColor:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    RED = "\033[31m"


def terminal_supports_color(stream: TextIO | None = None) -> bool:
    if stream is None:
        stream = sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return stream.isatty()


def colorize(text: str, *codes: str, stream: TextIO | None = None) -> str:
    if not codes or not terminal_supports_color(stream):
        return text
    return f"{''.join(codes)}{text}{TerminalColor.RESET}"


def stage_separator(stream: TextIO | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    line = colorize("=" * STAGE_SEPARATOR_WIDTH, TerminalColor.CYAN, stream=stream)
    for _ in range(3):
        print(line, file=stream)


def stage_message(status: str, stage_name: str, *codes: str, stream: TextIO | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    message = f"[pipeline] {status}: {stage_name}"
    print(colorize(message, TerminalColor.BOLD, *codes, stream=stream), file=stream)


@contextlib.contextmanager
def pipeline_stage(stage_name: str) -> Iterator[None]:
    stage_message("START", stage_name, TerminalColor.BLUE)
    try:
        yield
    except BaseException:
        stage_separator()
        stage_message("FAILED", stage_name, TerminalColor.RED, stream=sys.stderr)
        raise
    stage_separator()
    stage_message("DONE", stage_name, TerminalColor.GREEN)


def stage_log_slug(stage_name: str) -> str:
    return "-".join("".join(char if char.isalnum() else " " for char in stage_name).split()).lower()


def new_stage_log_path(directory: Path, stage_name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}-{stage_log_slug(stage_name)}"
    candidate = directory / f"{stem}.log"
    suffix = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{suffix}.log"
        suffix += 1
    return candidate


class TeeStream:
    def __init__(self, terminal: TextIO, log: TextIO) -> None:
        self.terminal = terminal
        self.log = log

    def write(self, data: str) -> int:
        self.terminal.write(data)
        self.log.write(data)
        return len(data)

    def flush(self) -> None:
        self.terminal.flush()
        self.log.flush()

    def isatty(self) -> bool:
        return self.terminal.isatty()


class StageLog:
    def __init__(self, logs_root: Path, stage_name: str, run_dir: Path | None = None) -> None:
        self.logs_root = logs_root
        self.stage_name = stage_name
        self.run_dir = run_dir
        self.path = new_stage_log_path((run_dir / "logs") if run_dir else (logs_root / ".pending"), stage_name)

    @contextlib.contextmanager
    def capture(self) -> Iterator[None]:
        try:
            with self.path.open("w", encoding="utf-8") as log_file:
                with contextlib.redirect_stdout(TeeStream(sys.stdout, log_file)), contextlib.redirect_stderr(
                    TeeStream(sys.stderr, log_file)
                ):
                    yield
        except BaseException:
            if self.run_dir is None:
                self.relocate(self.logs_root / "_failed")
            raise

    def relocate(self, destination_dir: Path) -> Path:
        destination = new_stage_log_path(destination_dir, self.stage_name)
        self.path.replace(destination)
        self.path = destination
        return destination

    def relocate_to_run(self, run_dir: Path) -> Path:
        return self.relocate(run_dir / "logs")


@contextlib.contextmanager
def captured_pipeline_stage(logs_root: Path, stage_name: str, run_dir: Path | None = None) -> Iterator[StageLog]:
    stage_log = StageLog(logs_root, stage_name, run_dir)
    with stage_log.capture():
        with pipeline_stage(stage_name):
            yield stage_log
