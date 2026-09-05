"""Exercise image preparation without a Docker daemon or Kubernetes cluster."""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HELPER = PROJECT_ROOT / "tests/support/local-images.sh"


def run_helper(tmp_path, command, *, failure="", skip=""):
    emulator = tmp_path / "emulator"
    for context in (
        "btc-node", "joinmarket-client-server", "irc-server",
        "wasabi-clients/2.0.4", "wasabi-clients/2.0.6", "wasabi-clients/2.6.0",
        "wasabi-backend/2.0.4", "wasabi-backend/2.6.0", "wasabi-coordinator/2.6.0",
    ):
        directory = emulator / "containers" / context
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Dockerfile").touch()
    log = tmp_path / "commands"
    env = {
        **os.environ,
        "COINJOIN_EMULATOR_SOURCE_DIR": str(emulator),
        "IMAGE_PREFIX": "test/",
        "COINJOIN_BTC_NODE_IMAGE": "",
        "SKIP_LOCAL_IMAGE_BUILD": skip,
        "KUBERNETES_IMAGE_PULL_POLICY": "Always",
        "COMMAND_LOG": str(log),
        "FAIL_COMMAND": failure,
    }
    result = subprocess.run(
        ["bash", "-c", '''
source "$1"
docker() {
  echo "docker $*" >> "$COMMAND_LOG"
  [[ "docker $*" != *"${FAIL_COMMAND:-NO_FAILURE}"* ]]
}
k3d() {
  echo "k3d $*" >> "$COMMAND_LOG"
  [[ "k3d $*" != *"${FAIL_COMMAND:-NO_FAILURE}"* ]]
}
''' + command, "test", str(HELPER)],
        env=env, text=True, capture_output=True, timeout=5,
    )
    return result, log.read_text() if log.exists() else ""


def test_joinmarket_build_needs_only_container_contexts(tmp_path):
    result, commands = run_helper(tmp_path, '''
local_images_build joinmarket || exit 1
local_images_import test-cluster || exit 1
echo "policy=$KUBERNETES_IMAGE_PULL_POLICY"
''')
    assert result.returncode == 0, result.stderr
    assert commands.count("docker build ") == 3
    assert commands.count("k3d image import ") == 3
    assert "test/joinmarket-client-server:latest" in commands
    assert "vendor" not in commands
    assert "JOINMARKET_BASE_IMAGE=" not in commands
    assert "policy=IfNotPresent" in result.stdout


@pytest.mark.parametrize("image", ["btc-node", "joinmarket-client-server", "irc-server"])
def test_failed_build_stops_even_when_caller_checks_status(tmp_path, image):
    result, commands = run_helper(
        tmp_path,
        "local_images_build joinmarket || exit 17\nlocal_images_import test-cluster",
        failure=f"docker build -t test/{image}:latest",
    )
    assert result.returncode == 17
    assert "FAIL: build" in result.stderr
    assert "k3d" not in commands


def test_failed_import_stops_test(tmp_path):
    result, _ = run_helper(tmp_path, '''
local_images_build joinmarket || exit 1
local_images_import test-cluster || exit 17
''', failure="k3d image import")
    assert result.returncode == 17
    assert "FAIL: k3d image import" in result.stderr


def test_missing_sources_stop_test(tmp_path):
    result, commands = run_helper(tmp_path, '''
COINJOIN_EMULATOR_SOURCE_DIR="$COINJOIN_EMULATOR_SOURCE_DIR/missing"
local_images_build joinmarket
''')
    assert result.returncode != 0
    assert "FAIL: emulator sources" in result.stderr
    assert not commands


def test_explicit_skip_uses_published_images(tmp_path):
    result, commands = run_helper(tmp_path, '''
local_images_build joinmarket || exit 1
local_images_import test-cluster
''', skip="1")
    assert result.returncode == 0
    assert not commands


@pytest.mark.parametrize("mode", ["local", "github"])
def test_suite_image_mode_reaches_infrastructure_builds(tmp_path, mode):
    source = (PROJECT_ROOT / "run-all.sh").read_text()
    # Execute the actual mode selection without launching the suite or builds.
    start = source.index('if [[ "${IMAGE_MODE}" == "local" ]]; then')
    end = source.index('if [[ "${RUN_SCENARIOS}"', start)
    selection = source[start:end]
    result, commands = run_helper(tmp_path, f'''
IMAGE_MODE={mode}
BUILD_IMAGES= PULL_IMAGES= PULL_ONLY=0 BUILD_ONLY=0
{selection}
local_images_build joinmarket || exit 1
local_images_import test-cluster
''')
    assert result.returncode == 0, result.stderr
    assert bool(commands) == (mode == "local")


@pytest.mark.parametrize("scenario, expected", [
    ({"default_version": "2.0.6"}, [
        "wasabi-client:2.0.6", "wasabi-backend:2.0.4",
    ]),
    ({"default_version": "2.0.4", "distributor_version": "2.0.6",
      "wallets": [{"version": "2.6.0"}]}, [
        "wasabi-client:2.0.4", "wasabi-client:2.0.6", "wasabi-client:2.6.0",
        "wasabi-backend:2.6.0", "wasabi-coordinator:2.6.0",
    ]),
])
def test_wasabi_images_follow_all_scenario_versions(tmp_path, scenario, expected):
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps(scenario))
    result, commands = run_helper(
        tmp_path, f"local_images_build wasabi {shlex.quote(str(scenario_file))}",
    )
    assert result.returncode == 0, result.stderr
    tags = [shlex.split(line)[3] for line in commands.splitlines()]
    assert tags == ["test/btc-node:latest", *(f"test/{image}" for image in expected)]


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("engine", ["joinmarket", "wasabi"])
def test_compose_prefetch_selects_base_only_for_local_joinmarket(tmp_path, local, engine):
    compose = yaml.safe_load((PROJECT_ROOT / "pipeline/compose.yaml").read_text())
    prefetch = compose["services"]["dind_image_prefetch"]
    values = {
        "COINJOIN_ENGINE": engine,
        "COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD": "1" if local else "",
        "COINJOIN_EMULATOR_IMAGE_PREFIX": "test/",
    }
    # Resolve Compose defaults while preserving escaped dollars for the shell.
    command = prefetch["command"].replace("$$", "\x00")
    command = re.sub(
        r"\$\{([A-Z_]+):-([^}]*)\}",
        lambda match: values.get(match[1]) or match[2], command,
    ).replace("\x00", "$")
    shell_body = shlex.split(command)[2]
    result = subprocess.run(
        ["bash", "-ec", '''
sleep() { :; }
docker() { echo "PULL $*"; }
''' + shell_body],
        env={**os.environ, "DOCKER_DEFAULT_PLATFORM": ""},
        text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    pulls = [line for line in result.stdout.splitlines() if line.startswith("PULL ")]
    if local:
        assert pulls == (
            ["PULL pull ghcr.io/ondrejman/joinmarket-base:latest"]
            if engine == "joinmarket" else []
        )
    else:
        assert "PULL pull test/btc-node" in pulls
        if engine == "joinmarket":
            assert "PULL pull test/joinmarket-client-server" in pulls
        assert not any("joinmarket-base" in line for line in pulls)
    assert any("COINJOIN_REGISTRY_CONFIG" in volume for volume in prefetch["volumes"])
