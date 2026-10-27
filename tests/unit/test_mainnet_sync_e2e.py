"""Check the real-mainnet E2E's contracts without starting its long-running services."""

import json
import os
from pathlib import Path
import re
import runpy
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.context import resolve_configuration
from coinjoin_pipeline.execution.mainnet_sync import configuration_errors

ROOT = Path(__file__).resolve().parents[2]
support = SimpleNamespace(**runpy.run_path(str(ROOT / "tests/support/mainnet_sync_e2e.py")))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.mark.parametrize(
    "mode,target,network",
    [
        ("mainnet", 1, "bitcoin"),
        ("mainnet", 100000, "bitcoin"),
        ("mainnet", 400000, "bitcoin"),
        ("mainnet", 401000, "bitcoin"),
        ("regtest", 1200, "bitcoin_regtest"),
    ],
)
def test_generated_configuration_obeys_the_production_sync_contract(tmp_path, mode, target, network):
    value = support.configuration(tmp_path, "http://127.0.0.1:9000", "sync-test", mode, target)
    config = resolve_configuration(PipelineConfiguration.from_mapping(value))
    assert configuration_errors(config) == []
    assert config.mainnet.network == network
    assert config.mainnet.target_height == target
    assert config.mainnet.scan_start_height == (None if mode == "mainnet" else 1)
    assert config.artifacts.uri == "s3://local-mainnet-sync/runs"
    assert config.pbs.blocksci_image == str(tmp_path / "images/blocksci.sif")


@pytest.mark.parametrize("target", [0, -1, 450000, 900000])
def test_test_configuration_cannot_request_a_full_mainnet(tmp_path, target):
    with pytest.raises(ValueError, match="height guard"):
        support.configuration(tmp_path, "http://127.0.0.1:9000", "sync-test", "mainnet", target)


def test_watchdog_rejects_an_ignored_target_or_an_excessive_download(tmp_path):
    progress = tmp_path / "data/runs/run/producer.json"
    save(progress, {"height": 900000})
    with pytest.raises(ValueError, match="height guard"):
        support.guard(tmp_path, "mainnet", 1)
    save(progress, {"height": 100000})
    data = tmp_path / "data/bitcoin/blocks/blk00000.dat"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="data guard"):
        support.guard(tmp_path, "mainnet", 1)


@pytest.mark.parametrize("height", [100000, 400000, 401000, 450000, 450001])
def test_watchdog_limits_core_height_independently_of_scanner_progress(tmp_path, height):
    save(tmp_path / "data/runs/run/producer.json", {"height": height, "scanner_height": 900000})
    if height > 450000:
        with pytest.raises(ValueError, match=f"Mainnet height guard exceeded: {height}"):
            support.guard(tmp_path, "mainnet", 1)
    else:
        support.guard(tmp_path, "mainnet", 1)


@pytest.fixture
def recovered(tmp_path):
    done = {
        "state": "done",
        "network": "bitcoin",
        "requested_target_height": 101000,
        "height": 101002,
        "dumplings_height": 101002,
        "hash": "a" * 64,
        "core_checkpoint": "s3://local-mainnet-sync/core/generations/run/manifest.json",
        "index_backup": "s3://local-mainnet-sync/index/generations/run/manifest.json",
    }
    save(tmp_path / "data/runs/local-sync-token-1/done.json", {"height": 100000})
    save(tmp_path / "data/runs/local-sync-token-2/done.json", done)
    (tmp_path / "recovered/bitcoin/chainstate").mkdir(parents=True)
    scanner = tmp_path / "recovered/dumplings/Scanner"
    scanner.mkdir(parents=True)
    (scanner / "Tx0Candidates.txt").touch()
    (scanner / "LastProcessedBlockHeight.txt").write_text("101002")
    save(
        tmp_path / "recovered-index-info.json",
        {
            "blocks": 101003,
            "height": 101002,
            "hash": done["hash"],
            "genesis": support.MAINNET_GENESIS,
        },
    )
    save(
        tmp_path / "recovered-core-info.json",
        {
            "blocks": 101002,
            "bestblockhash": done["hash"],
            "chain": "main",
            "pruned": True,
            "automatic_pruning": False,
        },
    )
    save(tmp_path / "recovered-core-indexes.json", {})
    save(tmp_path / "archived-oldest-block.json", {"size": 100})
    blocks = tmp_path / "data/bitcoin/blocks"
    blocks.mkdir(parents=True)
    for number in range(3):
        (blocks / f"blk{number:05d}.dat").write_bytes(b"block")
    save(tmp_path / "core-latest.json", {"height": 101002, "hash": done["hash"], "index_backup": done["index_backup"]})
    (tmp_path / "recovered-genesis.txt").write_text(support.MAINNET_GENESIS)
    return tmp_path


def test_mainnet_recovery_accepts_a_small_overshoot_and_records_evidence(recovered):
    result = support.check_final(recovered, "token", "mainnet", 101000)
    assert result["height"] == 101002
    assert result["recovery_verified"]
    assert result["block_archive_verified"]
    assert json.loads((recovered / "result.json").read_text()) == result


@pytest.mark.parametrize("count", [1, 2])
def test_bounded_mainnet_recovers_active_block_files_without_a_completed_archive(recovered, count):
    blocks = recovered / "data/bitcoin/blocks"
    for path in sorted(blocks.glob("blk*.dat"))[count:]:
        path.unlink()
    save(recovered / "archived-oldest-block.json", {"state": "pending"})
    restored = recovered / "recovered/bitcoin/blocks/blk00000.dat"
    restored.parent.mkdir(parents=True)
    restored.write_bytes(b"block")
    result = support.check_final(recovered, "token", "mainnet", 101000)
    assert result["recovery_verified"] and not result["block_archive_verified"]
    restored.unlink()
    with pytest.raises(FileNotFoundError):
        support.check_final(recovered, "token", "mainnet", 101000)


def test_completed_block_files_still_require_a_remote_archive(recovered):
    save(recovered / "archived-oldest-block.json", {"state": "pending"})
    with pytest.raises(KeyError, match="size"):
        support.check_final(recovered, "token", "mainnet", 101000)


def test_regtest_and_previously_archived_files_cannot_skip_the_archive_check(tmp_path):
    for chain, mode in (("bitcoin", "mainnet"), ("bitcoin/regtest", "regtest")):
        blocks = tmp_path / "data" / chain / "blocks"
        blocks.mkdir(parents=True)
        (blocks / "blk00000.dat").touch()
        assert support.requires_block_archive(tmp_path, mode) == (mode == "regtest")
    save(tmp_path / "data/archive-state/blk00000.dat.json", {"size": 100})
    assert support.requires_block_archive(tmp_path, "mainnet")


@pytest.mark.parametrize("broken", ["wrong-network", "wrong-hash", "no-increment", "wrong-backup"])
def test_recovery_fails_for_another_chain_stale_index_or_unpaired_backup(recovered, broken):
    path = recovered / "recovered-core-info.json"
    value = json.loads(path.read_text())
    if broken == "wrong-network":
        value["chain"] = "regtest"
    elif broken == "wrong-hash":
        value["bestblockhash"] = "b" * 64
    elif broken == "no-increment":
        save(recovered / "data/runs/local-sync-token-1/done.json", {"height": 101002})
    else:
        backup = recovered / "core-latest.json"
        record = json.loads(backup.read_text())
        record["index_backup"] = "s3://local-mainnet-sync/index/older/manifest.json"
        save(backup, record)
    save(path, value)
    with pytest.raises(AssertionError):
        support.check_final(recovered, "token", "mainnet", 101000)


def test_ci_diagnostics_keep_logs_and_results_without_copying_raw_data_or_credentials(recovered, tmp_path):
    (recovered / "credentials").write_text("do not export")
    (recovered / "images").mkdir()
    (recovered / "images/core.sif").write_bytes(b"image")
    log = recovered / "data/runs/local-sync-token-2/blocksci.log"
    log.write_text("parse failed")
    result = tmp_path / "artifacts"
    support.diagnostics(recovered, result, "mainnet", 1)
    assert (result / "exit-status").read_text() == "1\n"
    assert (result / "workers/local-sync-token-2/blocksci.log").read_text() == "parse failed"
    assert not (result / "credentials").exists()
    assert not (result / "images").exists()
    assert not (result / "recovered").exists()


def test_mainnet_ci_is_automatic_and_publishes_diagnostics_on_failure():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yaml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "push" in triggers and "pull_request" in triggers
    job = workflow["jobs"]["mainnet-sync-bounded"]
    assert job["needs"] == "pbs-apptainer-image" and "if" not in job
    assert job["env"]["MAINNET_TEST_TARGET_HEIGHT"] == "400000"
    run = next(step for step in job["steps"] if "test-mainnet-sync-mainnet.sh" in step.get("run", ""))
    assert "timeout " in run["run"]
    upload = next(step for step in job["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "github-test-results/mainnet-sync-mainnet/"
    checkouts = {step.get("with", {}).get("repository") for step in job["steps"]}
    assert {"OndrejMan/bitcoin-block-archive", "OndrejMan/Dumplings", "OndrejMan/blocksci"} <= checkouts


def test_mainnet_timeouts_cover_scanning_both_generations_and_cleanup(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yaml").read_text())
    job = workflow["jobs"]["mainnet-sync-bounded"]
    config = support.configuration(tmp_path, "http://127.0.0.1:9000", "sync-test", "mainnet", 400000)

    def seconds(value):
        hours, minutes, secs = map(int, value.split(":"))
        return hours * 3600 + minutes * 60 + secs

    producer = seconds(config["pbs"]["producer"]["walltime"])
    parser = seconds(config["pbs"]["blocksci"]["walltime"])
    # Core synchronization alone took 80 minutes in the failed 400000-block run.
    assert producer >= 6 * 3600
    generation = int(job["env"]["MAINNET_TEST_GENERATION_TIMEOUT"])
    assert generation >= producer + parser + 1800
    script = (ROOT / "tests/test-mainnet-sync-local.sh").read_text()
    assert f"MAINNET_TEST_GENERATION_TIMEOUT:-{generation}" in script
    command = next(step["run"] for step in job["steps"] if "test-mainnet-sync-mainnet.sh" in step.get("run", ""))
    test_timeout = int(re.search(r"(\d+)m\b", command).group(1)) * 60
    assert test_timeout >= 2 * generation + 3600
    assert job["timeout-minutes"] * 60 >= test_timeout + 3600


@pytest.mark.parametrize("target", ["0", "-1", "400001", "900000", "not-a-height"])
def test_invalid_mainnet_target_is_rejected_before_touching_docker(target):
    result = subprocess.run(
        ["bash", str(ROOT / "tests/test-mainnet-sync-mainnet.sh")],
        env={**os.environ, "MAINNET_TEST_TARGET_HEIGHT": target},
        capture_output=True,
        text=True,
        check=False,
        timeout=2,
    )
    assert result.returncode == 2
    assert "MAINNET_TEST_TARGET_HEIGHT" in result.stderr
