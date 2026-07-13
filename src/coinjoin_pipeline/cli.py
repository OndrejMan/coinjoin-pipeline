"""Installed thin CLI for the CoinJoin pipeline."""

from __future__ import annotations

from importlib.resources import as_file, files
import os
from pathlib import Path
import shlex
import sys

from . import MANIFEST_SCHEMA_VERSION, __version__
from .commands import action_from, launcher_command, option_value, validate_passthrough
from .doctor import check as doctor_check, validate_arguments
from .host import (
    add_effective_image_arguments,
    image_overrides,
    local_images,
    parse_host_options,
    required_image_components,
)
from .images import DEFAULT_VERSION, IMAGE_NAMES, Images, resolve_images
from .manifest import initial_manifest, mark_finished
from .process import run
from .runs import manifest_target, run_id_for, store_host_manifest, valid_run_id


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
        print(f"Pulling {image}")
        if run([runtime, "pull", image]):
            return 5
    return 0


def usage() -> None:
    print("""usage: coinjoin-pipeline [HOST OPTIONS] ACTION [PIPELINE OPTIONS]

Host actions: doctor, pull, version, builder
Pipeline actions: full-run, recreate, clean, analyze, export,
  coinjoin-analysis, pbs-from-s3, mappings, initialize, runs ..., scenarios ..., external ...

Host options:
  --version TAG                 coordinated image tag (default: latest)
  --runtime docker|podman       host container runtime
  --runs-root PATH              output root (default: ./coinjoin-runs)
  --local-build                 use local development image tags
  --pipeline-image IMAGE        override an individual image
  --emulator-image IMAGE
  --coinjoin-analysis-image IMAGE
  --blocksci-image IMAGE
  --mappings-image IMAGE
  --sake-image IMAGE
""")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] in {"-h", "--help"}:
        usage()
        return 0
    try:
        passthrough, host = parse_host_options(raw)
        runtime = str(host["runtime"])
        runs_root = Path(str(
            host.get("runs_root")
            or os.environ.get("EMULATION_LOGS_DIR")
            or Path.cwd() / "coinjoin-runs"
        )).expanduser().resolve()
        overrides = image_overrides(host)
        images = local_images() if host["local_build"] else resolve_images(host.get("version"), overrides)  # type: ignore[arg-type]
    except ValueError as exc:
        return fail(str(exc))

    top_action = passthrough[0] if passthrough else "full-run"
    if top_action == "version":
        print_version()
        return 0
    if top_action == "builder":
        from .builder import main as builder_main
        builder_main()
        return 0
    if top_action in {"doctor", "pull"}:
        if top_action == "doctor":
            doctor_arguments = passthrough[1:]
            errors = validate_arguments(doctor_arguments, runs_root)
            errors.extend(doctor_check(runtime, runs_root, images))
            if errors:
                for error in errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 2
            print(f"doctor OK: runtime={runtime} output={runs_root}")
            return 0
        return pull(runtime, images)

    action = action_from(passthrough)
    passthrough = add_effective_image_arguments(action, passthrough, images)
    required_images = required_image_components(action, passthrough)
    errors = validate_passthrough(passthrough, action)
    errors.extend(validate_arguments(passthrough, runs_root))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    reproduction = shlex.join(["coinjoin-pipeline", *raw])
    launcher_resource = files("coinjoin_pipeline").joinpath("resources/container/launcher.sh")
    with as_file(launcher_resource) as launcher:
        command = launcher_command(launcher, runtime, passthrough, images, runs_root, reproduction)
        pipeline_run_id: str | None = None
        if action in {"full-run", "recreate"} and not option_value(passthrough, "--run-dir"):
            candidate_run_id = run_id_for(passthrough)
            if valid_run_id(candidate_run_id):
                pipeline_run_id = candidate_run_id
                command.environment["PIPELINE_RUN_ID"] = candidate_run_id
        source_root = Path(__file__).resolve().parents[2]
        local_wrapper_root = source_root / "pipeline"
        if os.environ.get("PBS_FRONTEND_DIRECT") == "1" and (local_wrapper_root / "client/wrapper.py").is_file():
            command.environment["PBS_FRONTEND_WRAPPER_ROOT"] = str(local_wrapper_root)
        print(f"Generated runtime command:\n{command.rendered()}")
        direct_pbs = os.environ.get("PBS_FRONTEND_DIRECT") == "1" and not required_images
        preflight = [] if direct_pbs else doctor_check(
            runtime, runs_root, images, image_components=required_images,
        )
        if preflight:
            for error in preflight:
                print(f"ERROR: {error}", file=sys.stderr)
            return 2
        stage_pbs_dry_run = (
            (action == "analyze" and "--blocksciPbs" in passthrough)
            or (action == "coinjoin-analysis" and "--analysisPbs" in passthrough)
            or (action == "mappings" and "--mappingsPbs" in passthrough)
            or action == "pbs-from-s3"
            or (action == "recreate" and option_value(passthrough, "--artifact-backend") == "s3")
        )
        if "--dry-run" in passthrough and not stage_pbs_dry_run:
            print("[dry-run] validation passed; command was not executed")
            return 0
        target = manifest_target(action, passthrough, runs_root, pipeline_run_id)
        manifest = initial_manifest(
            action=action,
            requested_version=("local" if host["local_build"] else host.get("version") or DEFAULT_VERSION),
            effective_images=images.as_dict(),
            runtime=runtime,
            user_arguments=raw,
            pipeline_arguments=passthrough,
            user_command=reproduction,
            generated_runtime_command=command.rendered(),
            working_directory=str(Path.cwd()),
        )
        if target:
            store_host_manifest(target, manifest)
        exit_code = run(command.argv(), environment=command.environment)
        if target:
            mark_finished(manifest, exit_code)
            store_host_manifest(target, manifest)
        return exit_code if exit_code in {0, 2, 3, 4, 5, 130} else 5


if __name__ == "__main__":
    raise SystemExit(main())
