"""Host-only options and effective image argument policy."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal, TypedDict

from .commands import DOCKERLESS_RESEARCH_ACTIONS, has_option, option_value
from .images import IMAGE_NAMES, Images, validate_image


HostValueKey = Literal[
    "version", "runtime", "runs_root", "emulator", "coinjoin_analysis",
    "blocksci", "mappings", "sake",
]


class _RequiredHostOptions(TypedDict):
    runtime: str
    local_build: bool


class HostOptions(_RequiredHostOptions, total=False):
    version: str
    runs_root: str
    emulator: str
    coinjoin_analysis: str
    blocksci: str
    mappings: str
    sake: str


HOST_VALUE_OPTIONS: dict[str, HostValueKey] = {
    "--version": "version",
    "--runtime": "runtime",
    "--runs-root": "runs_root",
    "--emulator-image": "emulator",
    "--coinjoin-analysis-image": "coinjoin_analysis",
    "--blocksci-image": "blocksci",
    "--mappings-image": "mappings",
    "--sake-image": "sake",
}


def parse_host_options(argv: list[str]) -> tuple[list[str], HostOptions]:
    passthrough: list[str] = []
    host: HostOptions = {"runtime": "docker", "local_build": False}
    index = 0
    while index < len(argv):
        item = argv[index]
        for flag, key in HOST_VALUE_OPTIONS.items():
            if item == flag:
                if index + 1 >= len(argv):
                    raise ValueError(f"{flag} requires a value")
                host[key] = argv[index + 1]
                index += 2
                break
            if item.startswith(f"{flag}="):
                host[key] = item.split("=", 1)[1]
                index += 1
                break
        else:
            if item == "--local-build":
                host["local_build"] = True
                index += 1
            elif item == "container" and index + 1 < len(argv) and argv[index + 1] in {"docker", "podman"}:
                host["runtime"] = argv[index + 1]
                index += 2
            else:
                passthrough.append(item)
                index += 1
    return passthrough, host


def image_overrides(host: Mapping[str, object]) -> dict[str, str | None]:
    environment_names = {
        "emulator": "COINJOIN_EMULATOR_IMAGE",
        "coinjoin_analysis": "COINJOIN_ANALYSIS_IMAGE",
        "blocksci": "BLOCKSCI_IMAGE",
        "mappings": "MAPPINGS_ENUMERATOR_IMAGE",
        "sake": "SAKE_IMAGE",
    }
    return {
        component: (
            str(host[component]) if host.get(component)
            else os.environ.get(environment_names[component])
        )
        for component in IMAGE_NAMES
    }


def local_images(overrides: Mapping[str, str | None] | None = None) -> Images:
    defaults = Images(
        emulator="coinjoin-emulator:local",
        coinjoin_analysis="coinjoin-analysis:local",
        blocksci="blocksci-complete:local",
        mappings="coinjoin-mappings-enumerator:local",
        sake="coinjoin-mappings-sake:local",
    ).as_dict()
    for component in defaults:
        override = (overrides or {}).get(component)
        if override:
            validate_image(override)
            defaults[component] = override
    return Images(**defaults)


# The frontend submits Singularity references to PBS, so a stage delegated by
# one of these flags never touches the local Docker/Podman daemon. Only the
# delegated stage disappears, though: a shared-storage `full-run --analysisPbs`
# still emulates locally and must keep preflighting the emulator image.
PBS_DELEGATED_COMPONENTS = {
    "--analysisPbs": ("coinjoin_analysis",),
    "--blocksciPbs": ("blocksci",),
    "--mappingsPbs": ("mappings", "sake"),
}


def pbs_delegated_components(arguments: list[str]) -> set[str]:
    delegated: set[str] = set()
    for flag, components in PBS_DELEGATED_COMPONENTS.items():
        if has_option(arguments, flag):
            delegated.update(components)
    return delegated


def required_image_components(action: str, arguments: list[str]) -> set[str]:
    if action in DOCKERLESS_RESEARCH_ACTIONS:
        return set()
    if action == "pbs-from-s3":
        return set()
    if action == "full-run" and option_value(arguments, "--artifact-backend") == "s3":
        # Emulation runs in-cluster and analysis in PBS; nothing touches the
        # local Docker/Podman daemon.
        return set()
    if action == "clean":
        return set()
    delegated = pbs_delegated_components(arguments)
    if delegated and action not in {"full-run", "emulate"}:
        # A stage action with a PBS flag runs entirely on the compute node —
        # `analyze --blocksciPbs` never starts the local analysis pair. Only
        # full-run/emulate keep local work alongside a delegated stage.
        return set()
    if action in {"external analyze", "runs validate"}:
        # Both reopen a parsed chain with the BlockSci image.
        required = {"blocksci"}
    elif action == "emulate":
        required = {"emulator"}
    elif action == "coinjoin-analysis":
        required = {"coinjoin_analysis"}
    elif action in {"analyze", "export"}:
        required = {"blocksci", "coinjoin_analysis"}
    else:
        required = {"emulator", "coinjoin_analysis", "blocksci"}
        if has_option(arguments, "--mappingsPbs") or action == "mappings":
            required.update(("mappings", "sake"))
    return required - delegated


def add_effective_image_arguments(action: str, arguments: list[str], images: Images) -> list[str]:
    """Prevent wrapper defaults from silently reintroducing mutable latest tags."""
    result = list(arguments)
    if action == "external analyze" and not has_option(result, "--blocksci-image"):
        result.extend(("--blocksci-image", images.blocksci))
    pbs_images = (
        ("--blocksciPbs", "--pbs-blocksci-image", f"docker://{images.blocksci}"),
        ("--analysisPbs", "--pbs-coinjoin-analysis-image", f"docker://{images.coinjoin_analysis}"),
        ("--mappingsPbs", "--pbs-mappings-enumerator-image", f"docker://{images.mappings}"),
        ("--mappingsPbs", "--pbs-sake-image", f"docker://{images.sake}"),
    )
    for enabling_flag, image_flag, value in pbs_images:
        if has_option(result, enabling_flag) and not has_option(result, image_flag):
            result.extend((image_flag, value))
    return result
