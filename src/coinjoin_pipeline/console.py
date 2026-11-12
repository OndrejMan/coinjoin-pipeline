"""Exit codes and error output shared by the host commands."""

from __future__ import annotations

import sys

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILURE = 5
EXIT_INTERRUPTED = 130

# Execution reports a failed run ("[ERROR] ...") with EXIT_FAILURE
# (execution/pipeline_logging.exit_with_error), and a failed local container
# may pass its own exit code through.
EXIT_CODES = (
    (EXIT_OK, "success"),
    (EXIT_USAGE, "invalid configuration or arguments, or a failed preflight check"),
    (EXIT_FAILURE, "a failed run reported as [ERROR], a runtime error, or a failed pull"),
    (EXIT_INTERRUPTED, "interrupted"),
)


def fail(*messages: str, code: int = EXIT_USAGE) -> int:
    """Print each message as one ``ERROR:`` line on stderr and return the exit code."""
    for message in messages:
        print(f"ERROR: {message}", file=sys.stderr)
    return code
