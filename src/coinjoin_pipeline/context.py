"""Resolve one experiment and own its local execution lifetime."""

from __future__ import annotations

import os
import platform
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from .configuration import (
    ConfigurationError,
    PipelineConfiguration,
    config_value,
    replace_path,
)
from .host import image_overrides, local_images
from .images import Images, resolve_images
from .paths import EXPORTERS_ROOT, SCENARIOS_ROOT
from .runs import run_id_for
from .storage.s3 import (
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
    validate_s3_profile,
)


def resolve_configuration(config: PipelineConfiguration) -> PipelineConfiguration:
    try:
        ZoneInfo(config.run_timezone)
    except (KeyError, ValueError) as error:
        raise ConfigurationError(f"invalid run timezone: {config.run_timezone}") from error
    root = (
        Path(config.runs_root or os.environ.get("EMULATION_LOGS_DIR") or Path.cwd() / "coinjoin-runs")
        .expanduser()
        .resolve()
    )
    config = replace(config, runs_root=str(root))
    provided = set(config.provided)
    for stage in ("analysis", "blocksci", "mappings"):
        path = f"stages.{stage}"
        if getattr(config.pbs, stage).configured and path not in provided:
            config = replace_path(config, path, True)
            provided.add(path)
    for path, variable in (
        ("pbs.bitcoin_datadir", "PBS_BITCOIN_DATADIR"),
        ("images.uploader", "COINJOIN_UPLOADER_IMAGE"),
        ("images.unified_report", "COINJOIN_UNIFIED_REPORT_IMAGE"),
    ):
        if config_value(config, path) is None and os.environ.get(variable):
            config = replace_path(config, path, os.environ[variable])
            provided.add(path)
    if config.action == "pbs-from-s3":
        config = replace_path(config, "artifacts.backend", "s3")
    if config.engine == "joinmarket" and "coinjoin_type" not in provided:
        config = replace(config, coinjoin_type="joinmarket")
    if config.action in {"full-run", "emulate"} and not config.run_dir:
        config = replace(config, run_id=run_id_for(config))
        provided.add("run_id")
    config = replace(config, _provided=frozenset(provided))
    for path, validator in (
        ("artifacts.uri", validate_artifact_uri),
        ("artifacts.endpoint_url", validate_s3_endpoint_url),
        ("artifacts.credentials_file", validate_credentials_file),
        ("artifacts.profile", validate_s3_profile),
        ("run_id", validate_run_id),
        ("blocksci.cache_source_run_id", validate_run_id),
    ):
        value = config_value(config, path)
        if value is not None:
            config = replace_path(config, path, validator(value))
    if config.blocksci.script:
        config = replace_path(
            config,
            "blocksci.script",
            str(Path(config.blocksci.script).expanduser().resolve()),
        )
    config.validate()
    return config


@dataclass(frozen=True)
class RunContext:
    config: PipelineConfiguration
    images: Images
    environment: dict[str, str]
    run_dir: Path | None

    @classmethod
    def prepare(cls, config: PipelineConfiguration, reproduction: str) -> RunContext:
        config = resolve_configuration(config)
        overrides = image_overrides({item.name: getattr(config.images, item.name) for item in fields(Images)})
        images = (
            local_images(overrides) if config.images.local_build else resolve_images(config.images.version, overrides)
        )
        config = replace(
            config,
            images=replace(
                config.images,
                emulator=images.emulator,
                coinjoin_analysis=images.coinjoin_analysis,
                blocksci=images.blocksci,
                mappings=images.mappings,
                sake=images.sake,
            ),
        )
        pbs = config.pbs
        config = replace(
            config,
            pbs=replace(
                pbs,
                blocksci_image=pbs.blocksci_image or pbs.image or images.blocksci,
                coinjoin_analysis_image=pbs.coinjoin_analysis_image or pbs.image or images.coinjoin_analysis,
                mappings_enumerator_image=pbs.mappings_enumerator_image or pbs.image or images.mappings,
                sake_image=pbs.sake_image or pbs.image or images.sake,
            ),
        )
        from .execution.pbs_settings import (
            resolve_uploader_image,
            unified_report_image_reference,
        )

        if config.artifacts.backend == "s3":
            config = replace(
                config,
                images=replace(
                    config.images,
                    uploader=resolve_uploader_image(config),
                    unified_report=unified_report_image_reference(config),
                ),
            )
        root = config.runs_path
        run_dir = None
        if config.run_dir:
            selected = Path(config.run_dir).expanduser()
            run_dir = (selected if selected.is_absolute() else root / selected).resolve()
        elif config.run_id:
            run_dir = root / config.run_id
        environment = {
            "CONTAINER_RUNTIME": config.runtime,
            "EXPORTERS_DIR": str(EXPORTERS_ROOT),
            "SCENARIOS_DIR": str(SCENARIOS_ROOT),
            "NOTEBOOKS_DIR": str(root / ".notebooks"),
            "EMULATION_LOGS_DIR": str(root),
            "BLOCKSCI_LAUNCH_JUPYTER": os.environ.get("BLOCKSCI_LAUNCH_JUPYTER") or "0",
            "COINJOIN_EMULATOR_IMAGE": images.emulator,
            "COINJOIN_ANALYSIS_IMAGE": images.coinjoin_analysis,
            "BLOCKSCI_IMAGE": images.blocksci,
            "MAPPINGS_ENUMERATOR_IMAGE": images.mappings,
            "SAKE_IMAGE": images.sake,
            "REPRODUCTION_COMMAND": reproduction,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for key, value in (
            ("COINJOIN_UPLOADER_IMAGE", config.images.uploader),
            ("COINJOIN_UNIFIED_REPORT_IMAGE", config.images.unified_report),
        ):
            if value:
                environment[key] = value
        if config.run_id:
            environment["PIPELINE_RUN_ID"] = config.run_id
        if config.kubernetes.copy_to_host:
            environment["KUBERNETES_COPY_TO_HOST_DIR"] = os.environ.get("KUBERNETES_COPY_TO_HOST_DIR") or str(
                root / ".kubernetes-btc-data"
            )
        if config.engine == "joinmarket" and platform.machine() in {"arm64", "aarch64"}:
            environment["COINJOIN_EMULATOR_DOCKER_PLATFORM"] = (
                os.environ.get("COINJOIN_EMULATOR_DOCKER_PLATFORM") or "linux/amd64"
            )
        return cls(config, images, environment, run_dir)

    @contextmanager
    def activate(self):
        from .execution.locks import close_locks

        previous = dict(os.environ)
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        os.environ.update(self.environment)
        try:
            yield
        finally:
            close_locks()
            sys.dont_write_bytecode = previous_bytecode
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            os.environ.clear()
            os.environ.update(previous)
