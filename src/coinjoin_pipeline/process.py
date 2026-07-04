"""Small subprocess boundary shared by host-side orchestrators."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence


def run(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    replace: bool = False,
) -> int:
    """Execute an argv without a shell, optionally replacing this process."""
    argv = list(arguments)
    env = os.environ.copy()
    if environment:
        env.update(environment)
    if replace:
        os.execvpe(argv[0], argv, env)
    try:
        process = subprocess.Popen(argv, env=env)
    except OSError:
        return 127
    interrupted = False
    previous_term = signal.getsignal(signal.SIGTERM)

    def forward_termination(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        process.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_termination)
    try:
        try:
            return_code = process.wait()
        except KeyboardInterrupt:
            interrupted = True
            process.send_signal(signal.SIGINT)
            process.wait()
        return 130 if interrupted else return_code
    finally:
        signal.signal(signal.SIGTERM, previous_term)
