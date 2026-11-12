"""Load one experiment configuration and execute it in this process."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from exporters.artifact_paths import RUN_MANIFEST

from .arguments import (
    build_parser,
    load_configuration,
)
from .console import EXIT_FAILURE, EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, fail
from .context import RunContext
from .doctor import check as doctor_check
from .doctor import required_capabilities, required_image_components, validate_arguments
from .images import DEFAULT_VERSION
from .manifest import environment_snapshot, initial_manifest, mark_finished
from .paths import PIPELINE_ROOT
from .runs import read_host_manifest, store_host_manifest


def runtime_root() -> Path:
    if not (PIPELINE_ROOT / "compose.yaml").is_file():
        raise RuntimeError("Install coinjoin-pipeline with pip install -e . from its source checkout")
    return PIPELINE_ROOT


def use_line_buffered_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)


def print_dry_run_context(context: RunContext) -> None:
    """What a dry run resolved before execution: where the run lives and which images it uses."""
    print(f"[dry-run] run directory: {context.run_dir or '-'}")
    for component, image in context.images.as_dict().items():
        print(f"[dry-run] image {component}: {image}")


def execute(context: RunContext) -> None:
    from .execution import containers, orchestrator

    containers.install_termination_handlers()
    orchestrator.run_configuration(context.config)


def preflight_errors(context: RunContext) -> list[str]:
    """Option errors, then, for a real run, missing images and host capabilities."""
    config = context.config
    root = config.runs_path
    runtime_root()
    errors = validate_arguments(config, root)
    if not config.dry_run:
        errors.extend(
            doctor_check(
                config.runtime,
                root,
                context.images,
                image_components=required_image_components(config),
                capabilities=required_capabilities(config),
            )
        )
    return errors


def start_host_manifest(
    context: RunContext, original: list[str], reproduction: str
) -> tuple[Path | None, dict[str, object]]:
    """Create the runs root and record the launch in the run's manifest when the run has a directory.

    The command lock comes first, so a second command on a running run fails before it
    touches the run's record. A new run refuses a run ID that already records a launch.
    """
    config = context.config
    root = config.runs_path
    root.mkdir(parents=True, exist_ok=True)
    from .execution.locks import acquire_lock, command_lock_path

    # The orchestrator takes the same lock again; this process already holds it.
    acquire_lock(command_lock_path(config, root))
    target = None
    if context.run_dir is not None and (config.run_dir or config.action in {"emulate", "full-run"}):
        target = context.run_dir / RUN_MANIFEST
    if target and not config.run_dir and "host_launcher" in read_host_manifest(target):
        raise ValueError(
            f"run {config.run_id} already records a launch in {target}; start the new run under a fresh --run-id"
        )
    effective = asdict(config)
    effective.pop("_provided", None)
    manifest = initial_manifest(
        action=config.action,
        requested_version=config.images.version or DEFAULT_VERSION,
        effective_images=context.images.as_dict(),
        runtime=config.runtime,
        user_arguments=original,
        configuration=effective,
        environment=environment_snapshot({**os.environ, **context.environment}),
        user_command=reproduction,
        working_directory=str(Path.cwd()),
    )
    if target:
        store_host_manifest(target, manifest)
    return target, manifest


def main(argv: list[str] | None = None) -> int:
    use_line_buffered_output()
    original = list(sys.argv[1:] if argv is None else argv)
    if not original or original[0] in {"-h", "--help"}:
        build_parser().print_help()
        return EXIT_OK
    target = None
    manifest = None
    code = EXIT_FAILURE
    try:
        reproduction = shlex.join(["coinjoin-pipeline", *original])
        context = RunContext.prepare(load_configuration(original))
        config = context.config
        errors = preflight_errors(context)
        if errors:
            return fail(*errors)
        print(f"Run configuration: {config.action}")
        if config.dry_run:
            print_dry_run_context(context)
        else:
            target, manifest = start_host_manifest(context, original, reproduction)
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
    finally:
        if target and manifest is not None:
            mark_finished(manifest, code)
            store_host_manifest(target, manifest)
        from .execution.locks import close_locks

        # A command refused before execution still holds the lock start_host_manifest took.
        close_locks()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
