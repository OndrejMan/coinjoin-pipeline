"""Host-only options and effective image argument policy."""

from __future__ import annotations

import os

from .images import IMAGE_NAMES, Images


HOST_VALUE_OPTIONS = {
    "--version": "version",
    "--runtime": "runtime",
    "--runs-root": "runs_root",
    "--pipeline-image": "pipeline",
    "--emulator-image": "emulator",
    "--coinjoin-analysis-image": "coinjoin_analysis",
    "--blocksci-image": "blocksci",
    "--mappings-image": "mappings",
    "--sake-image": "sake",
}


def parse_host_options(argv: list[str]) -> tuple[list[str], dict[str, object]]:
    passthrough: list[str] = []
    host: dict[str, object] = {"runtime": "docker", "local_build": False}
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


def image_overrides(host: dict[str, object]) -> dict[str, str | None]:
    environment_names = {
        "pipeline": "WRAPPER_IMAGE",
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


def local_images() -> Images:
    return Images(
        pipeline="coinjoin-pipeline:local",
        emulator="coinjoin-emulator:local",
        coinjoin_analysis="coinjoin-analysis:local",
        blocksci="blocksci-complete:local",
        mappings="coinjoin-mappings-enumerator:local",
        sake="coinjoin-mappings-sake:local",
    )


def _artifact_backend(arguments: list[str]) -> str:
    for index, item in enumerate(arguments):
        if item == "--artifact-backend" and index + 1 < len(arguments):
            return arguments[index + 1]
        if item.startswith("--artifact-backend="):
            return item.split("=", 1)[1]
    return "shared-storage"


def required_image_components(action: str, arguments: list[str]) -> set[str]:
    if os.environ.get("PBS_FRONTEND_DIRECT") == "1" and any(
        flag in arguments for flag in ("--analysisPbs", "--blocksciPbs", "--mappingsPbs")
    ):
        # The frontend submits Singularity references to PBS. It does not run
        # these images through its local Docker/Podman daemon.
        return set()
    if action.startswith(("runs ", "scenarios ")):
        return {"pipeline"}
    if action == "external analyze":
        return {"pipeline", "blocksci"}
    if action == "recreate":
        return {"pipeline", "emulator"}
    if action == "pbs-from-s3":
        return set()
    if action == "full-run" and _artifact_backend(arguments) == "s3":
        # Emulation runs in-cluster and analysis in PBS; nothing touches the
        # local Docker/Podman daemon.
        return set()
    if action == "clean":
        return {"pipeline"}
    if action == "coinjoin-analysis":
        return {"pipeline", "coinjoin_analysis"}
    if action in {"analyze", "export"}:
        return {"pipeline", "blocksci", "coinjoin_analysis"}
    required = {"pipeline", "emulator", "coinjoin_analysis", "blocksci"}
    if "--mappingsPbs" in arguments or action == "mappings":
        required.update(("mappings", "sake"))
    return required


def add_effective_image_arguments(action: str, arguments: list[str], images: Images) -> list[str]:
    """Prevent wrapper defaults from silently reintroducing mutable latest tags."""
    result = list(arguments)
    if action == "external analyze" and "--blocksci-image" not in result:
        result.extend(("--blocksci-image", images.blocksci))
    pbs_images = (
        ("--blocksciPbs", "--pbs-blocksci-image", f"docker://{images.blocksci}"),
        ("--analysisPbs", "--pbs-coinjoin-analysis-image", f"docker://{images.coinjoin_analysis}"),
        ("--mappingsPbs", "--pbs-mappings-enumerator-image", f"docker://{images.mappings}"),
        ("--mappingsPbs", "--pbs-sake-image", f"docker://{images.sake}"),
    )
    for enabling_flag, image_flag, value in pbs_images:
        if enabling_flag in result and image_flag not in result:
            result.extend((image_flag, value))
    return result
