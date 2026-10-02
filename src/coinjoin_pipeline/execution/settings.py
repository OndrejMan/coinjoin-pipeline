"""Execution resource names and layout constants."""

from __future__ import annotations

from exporters.artifact_paths import EMULATION_MARKERS

from coinjoin_pipeline.images import DEFAULT_VERSION, IMAGE_NAMES
from coinjoin_pipeline.paths import PIPELINE_ROOT

ROOT_DIR = PIPELINE_ROOT
EMULATE_SCRIPT = ROOT_DIR / "emulate.sh"
ANALYSIS_SCRIPT = ROOT_DIR / "analysis.sh"
DELETE_SCRIPT = ROOT_DIR / "delete.sh"
COMPOSE_FILE = ROOT_DIR / "compose.yaml"
COMPOSE_PROJECT = "blocksci-emulator"
COMPOSE_PROJECT_ENV = "COINJOIN_COMPOSE_PROJECT"
COINJOIN_ANALYSIS_SOURCE_PATH_ENV = "COINJOIN_ANALYSIS_SOURCE_PATH"
COINJOIN_ANALYSIS_MOUNT_PATH_ENV = "COINJOIN_ANALYSIS_MOUNT_PATH"
COINJOIN_ANALYSIS_TARGET_PATH_ENV = "COINJOIN_ANALYSIS_TARGET_PATH"
COINJOIN_ANALYSIS_INPUT_DATA_PATH_ENV = "COINJOIN_ANALYSIS_INPUT_DATA_PATH"
DEFAULT_ENGINE = "wasabi"
DEFAULT_BLOCKSCI_IMAGE = IMAGE_NAMES["blocksci"] + ":" + DEFAULT_VERSION
DEFAULT_COINJOIN_ANALYSIS_IMAGE = IMAGE_NAMES["coinjoin_analysis"] + ":" + DEFAULT_VERSION
CONTAINER_SCENARIOS_DIR = "/mnt/scenarios"
DEFAULT_CONTAINER_SCENARIO = "/mnt/scenarios/overactive-local.json"
DEFAULT_JOINMARKET_CONTAINER_SCENARIO = "/mnt/scenarios/defaultJoinMarket.json"
DEFAULT_EMULATOR_IMAGE = IMAGE_NAMES["emulator"] + ":" + DEFAULT_VERSION
DEFAULT_K8S_CONTROL_IP = "host.docker.internal"
VALID_PULL_POLICIES = ("always", "missing", "never")
RUNS_ROOT_CONTAINER = "/runs/emulation/logs"
COINJOIN_ANALYSIS_SELECTED_ROOT_CONTAINER = "/runs/emulation/selected"
RUN_MARKER_FILES = EMULATION_MARKERS
IMAGE_PROVENANCE_ENV = {
    "BLOCKSCI_IMAGE": ("BLOCKSCI_IMAGE_ID", "BLOCKSCI_IMAGE_DIGEST"),
    "COINJOIN_ANALYSIS_IMAGE": (
        "COINJOIN_ANALYSIS_IMAGE_ID",
        "COINJOIN_ANALYSIS_IMAGE_DIGEST",
    ),
    "COINJOIN_EMULATOR_IMAGE": (
        "COINJOIN_EMULATOR_IMAGE_ID",
        "COINJOIN_EMULATOR_IMAGE_DIGEST",
    ),
}
PBS_SUBMIT_LOCK_NAME = ".pbs-submit.lock"
PEER_CONTAINERS = (
    "blocksci_analyzer",
    "coinjoin_analysis",
    "emulator_manager",
    "btc_data_wiper",
    "dind_image_prefetch",
    "isolated_docker_daemon",
)
