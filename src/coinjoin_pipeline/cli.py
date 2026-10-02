"""Load one experiment configuration and execute it in this process."""

from __future__ import annotations

import importlib
import os
import shlex
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from exporters.artifact_paths import RUN_MANIFEST

from . import MANIFEST_SCHEMA_VERSION, __version__
from .arguments import CONFIGURATION_FLAGS, action_word, build_parser, load_configuration
from .configuration import PipelineConfiguration
from .context import RunContext
from .doctor import check as doctor_check
from .doctor import required_capabilities, required_image_components, validate_arguments
from .images import DEFAULT_VERSION, IMAGE_NAMES, Images
from .manifest import environment_snapshot, initial_manifest, mark_finished
from .paths import PIPELINE_ROOT
from .process import run
from .runs import store_host_manifest


def fail(message: str, code: int = 2) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return code


def print_version() -> None:
    print(f"coinjoin-pipeline {__version__}")
    print(f"manifest schema: {MANIFEST_SCHEMA_VERSION}")
    print(f"default image version: {DEFAULT_VERSION}")
    for component, image in IMAGE_NAMES.items():
        print(f"{component}: {image}")


def pull(runtime: str, images: Images) -> int:
    for image in images.as_dict().values():
        if run([runtime, "pull", image]):
            return 5
    return 0


def runtime_root() -> Path:
    if not (PIPELINE_ROOT / "compose.yaml").is_file():
        raise RuntimeError("Install coinjoin-pipeline with pip install -e . from its source checkout")
    return PIPELINE_ROOT


def use_line_buffered_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)


def _utility(argv: list[str]) -> int | None:
    if argv[:1] == ["container"]:
        return None
    utility_actions = {
        "version",
        "doctor",
        "pull",
        "watch",
        "download-report",
        "clean-s3",
        "runs",
        "scenarios",
    }
    parser = build_parser(
        {
            "runtime",
            "runs_root",
            *{
                "images." + name
                for name in (
                    "version",
                    "local_build",
                    "emulator",
                    "coinjoin_analysis",
                    "blocksci",
                    "mappings",
                    "sake",
                    "uploader",
                    "unified_report",
                )
            },
        },
        add_help=False,
    )
    values, remaining = parser.parse_known_args(argv)
    supplied = vars(values)
    action = supplied.pop("action", None)
    supplied.pop("from_configuration", None)
    if action not in utility_actions:
        return None
    if action == "version":
        print_version()
        return 0
    context = RunContext.prepare(PipelineConfiguration.from_flat(supplied), shlex.join(["cjp", *argv]))
    root = context.config.runs_path
    if action in {"watch", "download-report", "clean-s3"}:
        module = importlib.import_module(f"coinjoin_pipeline.{action.replace('-', '_')}")
        return module.main(remaining, runs_root=root)
    if action in {"runs", "scenarios"}:
        from .execution.research import main as research_main

        if action == "runs" and remaining[:1] == ["validate"] and "blocksci_image" in supplied:
            remaining.extend(["--blocksci-image", context.images.blocksci])
        return research_main(
            [
                "--runs-root",
                str(root),
                "--runtime",
                context.config.runtime,
                action,
                *remaining,
            ]
        )
    if remaining:
        return fail(f"unexpected arguments for {action}: {' '.join(remaining)}")
    if action == "pull":
        return pull(context.config.runtime, context.images)
    errors = doctor_check(context.config.runtime, root, context.images)
    if errors:
        return fail("; ".join(errors))
    print(f"doctor OK: runtime={context.config.runtime} output={root}")
    return 0


def execute(context: RunContext) -> None:
    from .execution import containers, orchestrator

    containers.install_termination_handlers()
    if context.config.action == "external analyze":
        from .execution.research import run_external_configuration

        run_external_configuration(context.config)
    else:
        orchestrator.run_configuration(context.config)


def main(argv: list[str] | None = None) -> int:
    use_line_buffered_output()
    original = list(sys.argv[1:] if argv is None else argv)
    if not original or original[0] in {"-h", "--help"}:
        print(
            "usage: cjp run CONFIG.yaml [--dry-run]\n       cjp ACTION [OPTIONS]\n       cjp doctor | watch | download-report | clean-s3 | runs | scenarios"
        )
        return 0
    target = None
    manifest = None
    code = 5
    try:
        if (
            not any(value.split("=", 1)[0] in CONFIGURATION_FLAGS for value in original)
            and action_word(original) != "run"
        ):
            result = _utility(original)
            if result is not None:
                return result
        reproduction = shlex.join(["coinjoin-pipeline", *original])
        context = RunContext.prepare(load_configuration(original), reproduction)
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
        if errors:
            return fail("; ".join(errors))
        print(f"Run configuration: {config.action} ({config.driver}, {config.artifacts.backend})")
        if not config.dry_run:
            for path in (root, root / ".notebooks"):
                path.mkdir(parents=True, exist_ok=True)
            if context.run_dir is not None and (config.run_dir or config.action in {"emulate", "full-run"}):
                target = context.run_dir / RUN_MANIFEST
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
        with context.activate():
            execute(context)
        code = 0
    except (ValueError, OSError) as error:
        code = fail(str(error))
    except (RuntimeError, subprocess.CalledProcessError) as error:
        code = fail(str(error), 5)
    except KeyboardInterrupt:
        code = 130
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 2
    finally:
        if target and manifest is not None:
            mark_finished(manifest, code)
            store_host_manifest(target, manifest)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
