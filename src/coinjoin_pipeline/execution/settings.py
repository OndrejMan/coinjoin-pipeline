"""Execution resource names and layout constants."""

from __future__ import annotations

from coinjoin_pipeline.images import DEFAULT_VERSION, IMAGE_NAMES
from coinjoin_pipeline.paths import PIPELINE_ROOT

ROOT_DIR = PIPELINE_ROOT
EMULATE_SCRIPT = ROOT_DIR / "emulate.sh"
COMPOSE_PROJECT = "blocksci-emulator"
COMPOSE_PROJECT_ENV = "COINJOIN_COMPOSE_PROJECT"
DEFAULT_ENGINE = "wasabi"
CONTAINER_SCENARIOS_DIR = "/mnt/scenarios"
DEFAULT_CONTAINER_SCENARIO = "/mnt/scenarios/overactive-local.json"
DEFAULT_JOINMARKET_CONTAINER_SCENARIO = "/mnt/scenarios/defaultJoinMarket.json"
DEFAULT_EMULATOR_IMAGE = IMAGE_NAMES["emulator"] + ":" + DEFAULT_VERSION
