"""Run the wrapper image with Kubernetes emulation through the host runtime socket."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys

from .process import run


DEFAULT_LOCAL_IMAGE = "coinjoin-pipeline:local"
DEFAULT_IMAGE = "ghcr.io/ondrejman/coinjoin-pipeline:latest"
FORWARDED_ENVIRONMENT = (
    "WRAPPER_IMAGE",
    "BLOCKSCI_IMAGE",
    "BLOCKSCI_PULL_POLICY",
    "COINJOIN_ANALYSIS_IMAGE",
    "COINJOIN_ANALYSIS_PULL_POLICY",
    "COINJOIN_EMULATOR_IMAGE",
    "COINJOIN_EMULATOR_PULL_POLICY",
    "COINJOIN_EMULATOR_IMAGE_PREFIX",
    "COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD",
    "KUBERNETES_CONTROL_IP",
    "POST_WRAPPER_SHELL",
    "BLOCKSCI_LAUNCH_JUPYTER",
)


@dataclass(frozen=True)
class Configuration:
    runtime: str
    image: str
    kubeconfig: Path
    logs_dir: Path
    build: bool
    pipeline_arguments: tuple[str, ...]
    socket: Path
    source_root: Path | None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Run the pipeline wrapper with Kubernetes emulation. The host "
            "Docker or Podman API socket is mounted into the wrapper image."
        )
    )
    result.add_argument("--setup", required=True, type=Path, metavar="KUBECONFIG")
    result.add_argument("--logs-dir", type=Path)
    result.add_argument("--image", default=os.environ.get("PIPELINE_IMAGE"))
    result.add_argument("--runtime", choices=("docker", "podman"), default=os.environ.get("CONTAINER_RUNTIME", "docker"))
    result.add_argument("--build", action="store_true")
    result.add_argument("--source-root", type=Path, help="Repository checkout used by --build.")
    result.add_argument("pipeline_arguments", nargs=argparse.REMAINDER)
    return result


def configuration(argv: list[str] | None = None) -> Configuration:
    args = parser().parse_args(argv)
    kubeconfig = args.setup.expanduser().resolve()
    if not kubeconfig.is_file():
        raise ValueError(f"kubeconfig not found: {kubeconfig}")
    if shutil.which(args.runtime) is None:
        raise ValueError(f"container runtime {args.runtime!r} is not installed")
    default_logs = Path.cwd() / "coinjoin-runs"
    logs_dir = (args.logs_dir or Path(os.environ.get("EMULATION_LOGS_DIR", default_logs))).expanduser().resolve()
    image = args.image or (DEFAULT_LOCAL_IMAGE if args.build else DEFAULT_IMAGE)
    socket = Path(os.environ.get(
        "CONTAINER_SOCKET",
        "/var/run/docker.sock" if args.runtime == "docker"
        else f"/run/user/{os.getuid()}/podman/podman.sock",
    )).expanduser()
    source_root = args.source_root.expanduser().resolve() if args.source_root else None
    if args.build:
        source_root = source_root or (Path.cwd() if (Path.cwd() / "Dockerfile").is_file() else None)
        if source_root is None or not (source_root / "Dockerfile").is_file():
            raise ValueError("--build requires --source-root pointing to a coinjoin-pipeline checkout")
    arguments = tuple(args.pipeline_arguments or ("full-run", "--engine", "joinmarket"))
    return Configuration(args.runtime, image, kubeconfig, logs_dir, args.build, arguments, socket, source_root)


def build_command(config: Configuration, project_root: Path) -> list[str]:
    return [
        config.runtime,
        "build",
        "--tag",
        config.image,
        "--file",
        str(project_root / "Dockerfile"),
        str(project_root),
    ]


def runtime_command(config: Configuration) -> list[str]:
    command = [
        config.runtime,
        "run",
        "--rm",
        "--name",
        f"coinjoin-pipeline-{os.getpid()}",
        "-e",
        "KUBECONFIG=/root/.kube/config",
        "-e",
        "HOST_CLIENT_DIR=/app",
        "-e",
        "SCENARIOS_DIR=/app/scenarios",
        "-e",
        "EMULATION_LOGS_DIR=/runs",
        "-e",
        "EXPORTERS_DIR=/app/exporters",
        "-e",
        "CONTAINER_RUNTIME=docker",
    ]
    for name in FORWARDED_ENVIRONMENT:
        if name in os.environ:
            command.extend(("-e", f"{name}={os.environ[name]}"))
    command.extend((
        "-v",
        f"{config.kubeconfig}:/root/.kube/config:ro",
        "-v",
        f"{config.logs_dir}:/runs:rw",
        "-v",
        f"{config.socket}:/var/run/docker.sock",
        config.image,
        *config.pipeline_arguments,
        "--driver",
        "kubernetes",
        "--kubeconfig",
        "/root/.kube/config",
    ))
    return command


def main(argv: list[str] | None = None) -> int:
    try:
        config = configuration(argv)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    if config.build:
        assert config.source_root is not None
        build_result = run(build_command(config, config.source_root))
        if build_result:
            return 5
    return run(runtime_command(config))


if __name__ == "__main__":
    raise SystemExit(main())
