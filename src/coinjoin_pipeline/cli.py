"""Load one experiment configuration and execute it in this process."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .arguments import (
    build_parser,
    load_configuration,
)
from .console import EXIT_FAILURE, EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, fail
from .context import RunContext
from .paths import PIPELINE_ROOT


def runtime_root() -> Path:
    if not (PIPELINE_ROOT / "compose.yaml").is_file():
        raise RuntimeError("Install coinjoin-pipeline with pip install -e . from its source checkout")
    return PIPELINE_ROOT


def use_line_buffered_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)


def execute(context: RunContext) -> None:
    from .execution import orchestrator

    orchestrator.run_configuration(context.config)


def main(argv: list[str] | None = None) -> int:
    use_line_buffered_output()
    original = list(sys.argv[1:] if argv is None else argv)
    if not original or original[0] in {"-h", "--help"}:
        build_parser().print_help()
        return EXIT_OK
    code = EXIT_FAILURE
    try:
        context = RunContext.prepare(load_configuration(original))
        config = context.config
        runtime_root()
        print(f"Run configuration: {config.action}")
        config.runs_path.mkdir(parents=True, exist_ok=True)
        with context.activate():
            execute(context)
        code = EXIT_OK
    except (ValueError, OSError) as error:
        code = fail(str(error))
    except (RuntimeError, subprocess.CalledProcessError) as error:
        code = fail(str(error), code=EXIT_FAILURE)
    except KeyboardInterrupt:
        code = EXIT_INTERRUPTED
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else EXIT_USAGE
    return code


if __name__ == "__main__":
    raise SystemExit(main())
