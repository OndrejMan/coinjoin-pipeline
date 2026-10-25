"""Check the real-mainnet E2E's contracts without starting its long-running services."""

import json
import os
from pathlib import Path
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


@pytest.mark.parametrize("mode,target,network", [("mainnet", 100000, "bitcoin"), ("regtest", 1200, "bitcoin_regtest")])
def test_generated_configuration_obeys_the_production_sync_contract(tmp_path, mode, target, network):
    value = support.configuration(tmp_path, "http://127.0.0.1:9000", "sync-test", mode, target)
    config = resolve_configuration(PipelineConfiguration.from_mapping(value))
    assert configuration_errors(config) == []
    assert config.mainnet.network == network
    assert config.mainnet.target_height == target
    assert config.mainnet.scan_start_height == (None if mode == "mainnet" else 1)
    assert config.artifacts.uri == "s3://local-mainnet-sync/runs"
    assert config.pbs.blocksci_image == str(tmp_path / "images/blocksci.sif")


@pytest.mark.parametrize("target", [0, -1, 200000, 900000])
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
    save(tmp_path / "core-latest.json", {"height": 101002, "hash": done["hash"], "index_backup": done["index_backup"]})
    (tmp_path / "recovered-genesis.txt").write_text(support.MAINNET_GENESIS)
    return tmp_path


def test_mainnet_recovery_accepts_a_small_overshoot_and_records_evidence(recovered):
    result = support.check_final(recovered, "token", "mainnet", 101000)
    assert result["height"] == 101002
    assert result["recovery_verified"]
    assert json.loads((recovered / "result.json").read_text()) == result


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
    assert job["env"]["MAINNET_TEST_TARGET_HEIGHT"] == "100000"
    run = next(step for step in job["steps"] if "test-mainnet-sync-mainnet.sh" in step.get("run", ""))
    assert "timeout " in run["run"] and "60m" in run["run"]
    upload = next(step for step in job["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "github-test-results/mainnet-sync-mainnet/"
    checkouts = {step.get("with", {}).get("repository") for step in job["steps"]}
    assert {"OndrejMan/bitcoin-block-archive", "OndrejMan/Dumplings", "OndrejMan/blocksci"} <= checkouts


@pytest.mark.parametrize("target", ["0", "-1", "150001", "900000", "not-a-height"])
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
