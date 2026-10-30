from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import os
import shutil
import sys

import pytest

from coinjoin_pipeline.configuration import ConfigurationError, PipelineConfiguration
from coinjoin_pipeline.context import resolve_configuration
from coinjoin_pipeline.execution import mainnet_runtime, mainnet_sync, s3_markers
from coinjoin_pipeline.execution.mainnet_runtime import (
    Worker,
    require_ready,
    resume_file,
    scanner_complete,
    atomic_json,
    resume_signature,
)


@pytest.fixture
def config(tmp_path):
    return resolve_configuration(
        PipelineConfiguration.from_mapping(
            {
                "action": "mainnet-sync",
                "run_id": "mainnet-sync-local-1",
                "runs_root": str(tmp_path / "runs"),
                "artifacts": {
                    "endpoint_url": "http://127.0.0.1:9000",
                    "credentials_file": str(tmp_path / "credentials"),
                    "profile": "local",
                },
                "mainnet": {
                    "root": str(tmp_path / "core"),
                    "state_uri": "s3://local/sync",
                    "backup_uri": "s3://local/core",
                    "blocks_uri": "s3://local/blocks",
                    "dumplings_uri": "s3://local/dumps",
                },
                "blocksci": {"index_dir": str(tmp_path / "index"), "index_backup_uri": "s3://local/index"},
            }
        )
    )


def test_four_scripts_are_self_contained_and_syntactically_valid(config):
    locks = {"restore": "index_lock_exclusive", "blocksci": "index_lock_exclusive", "checkpoint": "index_lock_shared"}
    for stage in mainnet_sync.SYNC_STAGES:
        script = mainnet_sync.render_sync(config, stage)
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        assert "def scanner_complete" in script
        if stage in locks:
            assert "INDEX_LOCK_WAIT=0" in script and f"\n{locks[stage]}\n" in script
        else:
            assert "rm -rf" not in script
        assert script.rstrip().endswith(f'"$WORK/settings.json" {stage}')
        assert "clean_scratch" not in script
        assert '"log_root": ' + json.dumps(str(config.runs_path / config.run_id / "logs")) in script
    assert not config.stages.blocksci


def test_checkpoint_job_defaults_to_little_memory(config):
    resources = {stage: mainnet_sync.resources(config, stage) for stage in mainnet_sync.SYNC_STAGES}
    assert resources["blocksci"].mem == "256gb"
    assert resources["checkpoint"].mem == resources["restore"].mem == "8gb"
    assert resources["checkpoint"].ncpus == 16


def test_every_job_must_use_the_same_multijob(config):
    changed = replace(config, pbs=replace(config.pbs, checkpoint=replace(config.pbs.checkpoint, multijob="brno")))
    assert "All jobs must request the same multijob location" in mainnet_sync.configuration_errors(changed)


def test_dependencies_are_persisted_and_rolled_back(config, monkeypatch):
    calls, cancelled = [], []

    def submit(job, _dry):
        calls.append(job)
        if len(calls) == 2:
            raise RuntimeError("scheduler refused second job")
        return "101.local"

    monkeypatch.setattr(mainnet_sync, "submit_pbs_job", submit)
    monkeypatch.setattr(s3_markers, "qdel_pbs_job", lambda job: cancelled.append(job) or True)
    with pytest.raises(RuntimeError, match="scheduler refused"):
        mainnet_sync.submit_sync(config)
    assert [job.stage for job in calls] == ["mainnet-restore", "mainnet-producer"]
    assert calls[1].dependencies == ("101.local",)
    assert cancelled == ["101.local"]
    assert (config.runs_path / config.run_id / ".pbs/mainnet-restore.jobid").read_text().strip() == "101.local"


def test_jobs_are_chained_in_order(config, monkeypatch):
    calls = []
    monkeypatch.setattr(
        mainnet_sync, "submit_pbs_job", lambda job, _dry: calls.append(job) or f"{100 + len(calls)}.local"
    )
    mainnet_sync.submit_sync(config)
    assert [job.stage for job in calls] == [f"mainnet-{stage}" for stage in mainnet_sync.SYNC_STAGES]
    assert [job.dependencies for job in calls] == [(), ("101.local",), ("102.local",), ("103.local",)]


def test_ready_marker_must_match_exact_generation():
    ready = {
        "state": "ready",
        "run_id": "old",
        "height": 500,
        "hash": "a" * 64,
        "dumplings_height": 500,
        "dumplings_backup": "s3://local/old",
    }
    with pytest.raises(ValueError, match="exact generation"):
        require_ready(ready, "new")
    assert require_ready(ready, "old") == 500


def test_missing_and_unresolved_dumplings_state_refuses_publication(tmp_path):
    for name in ("Checkpoint.txt", "Tx0Candidates.txt", "Tx0CandidateMisses.txt"):
        (tmp_path / name).write_text("")
    (tmp_path / "LastProcessedBlockHeight.txt").write_text("500")
    atomic_json(tmp_path / "ChainCheckpoint.json", {"height": 500, "hash": "a" * 64})
    (tmp_path / "Checkpoint.txt").write_text("500\nTx0Candidates.txt 0\nTx0CandidateMisses.txt 0\n")
    scanner_complete(tmp_path, 500)
    (tmp_path / "Tx0CandidateMisses.txt").write_text("400 deadbeef unresolved\n")
    with pytest.raises(ValueError, match="unresolved"):
        scanner_complete(tmp_path, 500)
    (tmp_path / "Tx0CandidateMisses.txt").unlink()
    with pytest.raises(ValueError, match="missing"):
        scanner_complete(tmp_path, 500)


def test_resume_requires_real_file_and_offset(tmp_path):
    index, blocks = tmp_path / "index", tmp_path / "blocks"
    blocks.mkdir()
    atomic_json(
        index / "parsed/parser/blockList.dat.resume.json",
        {"schema_version": 1, "file_number": 7, "data_offset": 8, "block_size": 80},
    )
    with pytest.raises(ValueError, match="missing or truncated"):
        resume_file(index, blocks)
    (blocks / "blk00007.dat").write_bytes(bytes(88))
    assert resume_file(index, blocks) == 7
    original = resume_signature(index, blocks)
    with (blocks / "blk00007.dat").open("ab") as handle:
        handle.write(b"a later block")
    assert resume_signature(index, blocks) == original
    (blocks / "blk00007.dat").write_bytes(b"changed!" + bytes(100))
    assert resume_signature(index, blocks) != original


def test_nested_working_directories_are_rejected(config):
    changed = replace(config, blocksci=replace(config.blocksci, index_dir=config.mainnet.root + "/index"))
    assert any("nesting" in message for message in mainnet_sync.configuration_errors(changed))


def test_scan_start_override_is_restricted_to_regtest(config):
    changed = replace(config, mainnet=replace(config.mainnet, scan_start_height=1))
    assert any("isolated regtest" in message for message in mainnet_sync.configuration_errors(changed))
    regtest = replace(changed, mainnet=replace(changed.mainnet, network="bitcoin_regtest"))
    assert mainnet_sync.configuration_errors(regtest) == []


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "200000"])
def test_invalid_sync_target_is_rejected(value):
    with pytest.raises(ConfigurationError):
        PipelineConfiguration.from_mapping({"mainnet": {"target_height": value}})


def test_sync_target_is_embedded_in_every_job(config):
    bounded = replace(config, mainnet=replace(config.mainnet, target_height=200000))
    assert mainnet_sync.configuration_errors(bounded) == []
    for stage in mainnet_sync.SYNC_STAGES:
        assert '"target_height": 200000' in mainnet_sync.render_sync(bounded, stage)


@pytest.mark.parametrize("target", [100, 90])
def test_bounded_sync_freezes_actual_tip_during_initial_download(worker, monkeypatch, target):
    worker.c["target_height"] = target
    snapshots = iter(
        [
            {"blocks": 50, "headers": 900000, "initialblockdownload": True},
            {"blocks": 100, "headers": 900000, "initialblockdownload": True},
            {"blocks": 102, "bestblockhash": "b" * 64},
        ]
    )
    calls, progress, sleeps = [], [], []

    def rpc(method, *args):
        calls.append((method, args))
        return next(snapshots) if method == "getblockchaininfo" else None

    worker.core = type("Core", (), {"poll": lambda _: None})()
    monkeypatch.setattr(worker, "rpc", rpc)
    monkeypatch.setattr(worker, "stop_core", lambda: None)
    monkeypatch.setattr(worker, "start_core", lambda offline: None)
    monkeypatch.setattr(worker, "progress", lambda *args, **kw: progress.append((args, kw)))
    monkeypatch.setattr("coinjoin_pipeline.execution.mainnet_runtime.time.sleep", sleeps.append)
    assert worker.synchronize_core() == {"blocks": 102, "bestblockhash": "b" * 64}
    assert ("setnetworkactive", ("false",)) in calls
    assert not any(method == "getnetworkinfo" for method, _ in calls)
    assert progress[0][0] == ("core-sync", 50, target)
    assert progress[0][1]["target_height"] == target
    assert sleeps == [1]


def test_unbounded_sync_still_waits_for_initial_download_and_headers(worker, monkeypatch):
    snapshots = iter(
        [
            {"blocks": 100, "headers": 100, "initialblockdownload": True},
            {"blocks": 100, "headers": 101, "initialblockdownload": False},
            {"blocks": 101, "headers": 101, "initialblockdownload": False},
            {"blocks": 101, "bestblockhash": "b" * 64},
        ]
    )
    worker.core = type("Core", (), {"poll": lambda _: None})()
    monkeypatch.setattr(
        worker, "rpc", lambda method, *args: next(snapshots) if method == "getblockchaininfo" else {"connections": 1}
    )
    monkeypatch.setattr(worker, "progress", lambda *args, **kw: None)
    monkeypatch.setattr(worker, "stop_core", lambda: None)
    monkeypatch.setattr(worker, "start_core", lambda offline: None)
    sleeps = []
    monkeypatch.setattr("coinjoin_pipeline.execution.mainnet_runtime.time.sleep", sleeps.append)
    assert worker.synchronize_core()["blocks"] == 101
    assert len(sleeps) == 2


def test_existing_higher_chain_is_frozen_without_rollback(worker, monkeypatch):
    worker.c["target_height"] = 100
    calls = []

    def rpc(method, *args):
        calls.append(method)
        return {"blocks": 150, "bestblockhash": "b" * 64}

    monkeypatch.setattr(worker, "rpc", rpc)
    monkeypatch.setattr(worker, "progress", lambda *args, **kw: None)
    monkeypatch.setattr(worker, "stop_core", lambda: calls.append("stop"))
    monkeypatch.setattr(worker, "start_core", lambda offline: calls.append(("start", offline)))
    assert worker.synchronize_core()["blocks"] == 150
    assert calls == ["getblockchaininfo", "setnetworkactive", "stop", ("start", True), "getblockchaininfo"]


def test_frozen_height_includes_blocks_validated_after_networking_was_disabled(worker, monkeypatch):
    worker.c["target_height"] = 400000
    state = {"height": 400013, "network": True, "stopped": False}

    def rpc(method, *args):
        if method == "setnetworkactive":
            assert args == ("false",)
            state.update(network=False, height=400019)
        elif method == "getblockchaininfo":
            return {"blocks": state["height"], "bestblockhash": str(state["height"])}
        else:
            raise AssertionError(method)

    def stop():
        assert not state["network"]
        state.update(stopped=True, height=400022)

    def start(offline):
        assert offline and state["stopped"]
        state["stopped"] = False

    monkeypatch.setattr(worker, "rpc", rpc)
    monkeypatch.setattr(worker, "stop_core", stop)
    monkeypatch.setattr(worker, "start_core", start)
    monkeypatch.setattr(worker, "progress", lambda *args, **kw: None)
    assert worker.synchronize_core() == {"blocks": 400022, "bestblockhash": "400022"}
    assert not state["stopped"] and not state["network"]


@pytest.mark.parametrize("height", [100000, 500000])
def test_producer_reports_core_height_separately_from_scanner_progress(worker, monkeypatch, height):
    worker.c["target_height"] = height
    block_hash = "a" * 64
    reports = []
    monkeypatch.setattr(worker, "start_core", lambda _: None)
    monkeypatch.setattr(worker, "stop_core", lambda: None)
    monkeypatch.setattr(worker, "synchronize_core", lambda: {"blocks": height, "bestblockhash": block_hash})
    monkeypatch.setattr(worker, "rpc", lambda method, *args: block_hash if method == "getblockhash" else height)
    monkeypatch.setattr(worker, "container_command", lambda *args, **kwargs: list(args))
    monkeypatch.setattr(worker, "archive", lambda: None)
    monkeypatch.setattr(worker, "publish_dumplings", lambda _: "s3://local/dumps/manifest.json")
    monkeypatch.setattr(worker, "publish_json", lambda *args: None)

    def scan(command, name, poll):
        assert name == "dumplings"
        assert "--include-tip" in command and f"--frozen-height={height}" in command
        scanner = worker.dumps / "Scanner"
        progress = scanner / "LastProcessedBlockHeight.txt"
        for checkpoint in (None, "invalid", str(worker.scan_start - 1), str(height)):
            if checkpoint is not None:
                progress.write_text(checkpoint)
            poll()
            reports.append(json.loads((worker.run / "producer.json").read_text()))
        (scanner / "Tx0Candidates.txt").touch()
        (scanner / "Tx0CandidateMisses.txt").touch()
        (scanner / "Checkpoint.txt").write_text(f"{height}\nTx0Candidates.txt 0\nTx0CandidateMisses.txt 0\n")

    monkeypatch.setattr(worker, "run_command", scan)
    first_synchronization(worker)
    worker.producer()
    assert [report["height"] for report in reports] == [height] * 4
    assert [report["scanner_height"] for report in reports] == [worker.scan_start - 1] * 3 + [height]
    assert [report["current"] for report in reports] == [0] * 3 + [max(0, height - worker.scan_start + 1)]
    assert all(report["total"] == max(0, height - worker.scan_start + 1) for report in reports)
    ready = json.loads((worker.run / "ready.json").read_text())
    assert ready["height"] == ready["dumplings_height"] == height


@pytest.mark.parametrize("chain,height", [("main", 99999), ("regtest", 99)])
def test_short_historical_chain_keeps_all_blocks(worker, monkeypatch, chain, height):
    worker.chain = chain
    monkeypatch.setattr(worker, "run_command", lambda *args: pytest.fail("Core cannot prune this short chain"))
    worker.archive(prune=True, height=height, number=0)


def first_synchronization(worker):
    """What the restore job leaves for a producer that builds from scratch."""
    atomic_json(worker.run / "restore.json", {"run_id": worker.run_id, "action": "skipped-no-backup"})


@pytest.fixture
def worker(config, tmp_path, monkeypatch):
    fake = Path(__file__).resolve().parents[1] / "support/persistent-index"
    monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_S3", str(tmp_path / "s3"))
    settings = {
        **asdict(config.mainnet),
        **dict(
            root=config.mainnet.root,
            index_dir=config.blocksci.index_dir,
            run_id=config.run_id,
            log_root=str(config.runs_path / config.run_id / "logs"),
            endpoint=config.artifacts.endpoint_url,
            credentials=config.artifacts.credentials_file,
            profile=config.artifacts.profile,
            pbs_resources={"ncpus": 1, "mem": "1gb"},
        ),
        "blocksci_image": "local.sif",
        "backup_uri": config.mainnet.backup_uri,
        "index_backup_uri": config.blocksci.index_backup_uri,
        "poll_seconds": 1,
    }
    work = tmp_path / "work"
    work.mkdir()
    return Worker(settings, work)


def test_component_output_is_visible_to_frontend_before_command_exits(worker):
    worker.stage = "producer"
    worker.c["poll_seconds"] = 0.01
    release = worker.work / "release"
    seen = []
    command = [
        sys.executable,
        "-c",
        """
import pathlib, sys, time
print('core stdout', flush=True)
print('core stderr', file=sys.stderr, flush=True)
release = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 2
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
sys.exit(0 if release.exists() else 1)
""",
        str(release),
    ]

    def observe():
        log = worker.logs / "core-producer.log"
        text = log.read_text()
        if "core stderr" in text:
            seen.append(text)
            release.touch()

    worker.run_command(command, "core-producer", observe)
    assert seen and "core stdout" in seen[0]
    assert worker.logs == Path(worker.c["log_root"]) / "mainnet-producer"
    assert not (worker.run / "core-producer.log").exists()


def test_parser_progress_and_s3_upload_use_the_shared_component_logs(worker, monkeypatch):
    worker.stage = "blocksci"
    worker.logs.mkdir(parents=True)
    (worker.logs / "blocksci.log").write_text("50% parsing blocks\n")
    producer = Path(worker.c["log_root"]) / "mainnet-producer"
    producer.mkdir()
    (producer / "core-producer.log").write_text("other stage\n")
    progress, uploaded = [], []
    monkeypatch.setattr(worker, "progress", lambda *args, **kwargs: progress.append((args, kwargs)))
    monkeypatch.setattr(worker, "upload", lambda *args: uploaded.append(args))
    worker.parse_progress()
    worker.publish_logs()
    assert progress == [(("blocksci-parse", 50.0, 100), {"detail": "parsing blocks"})]
    assert uploaded == [(worker.logs / "blocksci.log", f"{worker.remote}/logs/{worker.run_id}/blocksci.log")]


@pytest.mark.parametrize("readonly,index_writable", [(False, False), (True, True)])
def test_container_binds_symlink_targets_without_changing_workload_paths(worker, tmp_path, readonly, index_writable):
    storage = tmp_path / "beegfs"
    storage.mkdir()
    alias = tmp_path / "multijob-brno"
    alias.symlink_to(storage, target_is_directory=True)
    worker.root, worker.work, worker.index = (alias / name for name in ("dataset", "work", "index"))
    for path in (worker.root, worker.work, worker.index):
        path.mkdir()
    worker.container = "apptainer"
    datadir = str(worker.root / "bitcoin")
    command = worker.container_command(
        worker.c["blocksci_image"], "reader", datadir, readonly=readonly, index_writable=index_writable
    )
    binds = [command[index + 1] for index, argument in enumerate(command) if argument == "--bind"]
    for path, ro in ((worker.root, readonly), (worker.work, False), (worker.index, not index_writable)):
        source = path.resolve()
        suffix = ":ro" if ro else ""
        assert f"{source}:{source}{suffix}" in binds
        assert f"{source}:{path}{suffix}" in binds
    assert command[-3:] == [worker.c["blocksci_image"], "reader", datadir]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_worker_uploads_logs_even_when_cleanup_fails(worker, monkeypatch, cleanup_fails):
    settings = worker.work / "settings.json"
    settings.write_text("{}")
    uploaded = []

    def producer():
        raise RuntimeError("Core failed to start")

    def cleanup():
        if cleanup_fails:
            raise RuntimeError("Core did not shut down cleanly")

    monkeypatch.setattr(mainnet_runtime, "Worker", lambda *_: worker)
    monkeypatch.setattr(worker, "producer", producer)
    monkeypatch.setattr(worker, "cleanup", cleanup)
    monkeypatch.setattr(worker, "publish_logs", lambda: uploaded.append(True))
    message = "Core did not shut down cleanly" if cleanup_fails else "Core failed to start"
    with pytest.raises(RuntimeError, match=message):
        mainnet_runtime.main([str(settings), "producer"])
    assert uploaded == [True]
    assert (worker.run / "producer.quiescent").exists() is not cleanup_fails


@pytest.mark.parametrize("foreign_owner", [False, True])
def test_failed_core_allows_failure_hook_to_release_only_its_dataset(worker, monkeypatch, foreign_owner):
    settings = worker.work / "settings.json"
    settings.write_text("{}")
    worker.core = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    assert worker.core.wait(timeout=3) == 1
    uploaded = []

    def producer():
        worker.acquire_dataset()
        raise RuntimeError("Core failed to start")

    monkeypatch.setattr(mainnet_runtime, "Worker", lambda *_: worker)
    monkeypatch.setattr(worker, "producer", producer)
    monkeypatch.setattr(worker, "publish_logs", lambda: uploaded.append(True))
    monkeypatch.setattr(worker, "progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "publish_json", lambda *args: None)
    with pytest.raises(RuntimeError, match="Core failed to start"):
        mainnet_runtime.main([str(settings), "producer"])
    assert worker.core is None
    assert (worker.run / "producer.quiescent").is_file()
    assert not (worker.run / "done.json").exists()
    assert uploaded == [True]
    assert worker.active.exists()
    if foreign_owner:
        atomic_json(worker.active / "owner.json", {"run_id": "another-run"})
    mainnet_runtime.main([str(settings), "failed", "mainnet-producer", "1"])
    assert worker.active.exists() is foreign_owner


def test_checkpoint_shutdown_still_rejects_failed_core(worker):
    worker.core = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    assert worker.core.wait(timeout=3) == 1
    with pytest.raises(RuntimeError, match="Core did not shut down cleanly"):
        worker.stop_core()
    assert worker.core is None


def test_core_shutdown_timeout_keeps_dataset_locked(worker, monkeypatch):
    class RunningCore:
        def poll(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("bitcoind", timeout)

    settings = worker.work / "settings.json"
    settings.write_text("{}")
    worker.core = RunningCore()
    worker.acquire_dataset()
    uploaded = []

    def producer():
        raise RuntimeError("producer failed")

    monkeypatch.setattr(mainnet_runtime, "Worker", lambda *_: worker)
    monkeypatch.setattr(worker, "producer", producer)
    monkeypatch.setattr(worker, "rpc", lambda *args: None)
    monkeypatch.setattr(worker, "publish_logs", lambda: uploaded.append(True))
    monkeypatch.setattr(worker, "progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "publish_json", lambda *args: None)
    with pytest.raises(subprocess.TimeoutExpired):
        mainnet_runtime.main([str(settings), "producer"])
    assert worker.core.poll() is None
    assert not (worker.run / "producer.quiescent").exists()
    assert uploaded == [True]
    mainnet_runtime.main([str(settings), "failed", "mainnet-producer", "1"])
    assert worker.active.exists()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_backup_replaces_the_previous_one_without_readback(worker, tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "chainstate").mkdir(parents=True)
    (source / "chainstate/state.dat").write_bytes(b"a consistent Core snapshot")
    worker.env["FAKE_S5CMD_FORBID_ARCHIVE_CAT"] = "1"
    uri = worker.backup(source, ["chainstate"], "s3://local/core", {"height": 500}, "checkpoint.tar.zst")
    assert uri == "s3://local/core/manifest.json"
    manifest = json.loads((tmp_path / "s3" / uri.removeprefix("s3://")).read_text())
    archive = tmp_path / "s3" / manifest["archive_uri"].removeprefix("s3://")
    assert manifest["archive_size"] == archive.stat().st_size
    assert manifest["member_bytes"] == {"chainstate": len(b"a consistent Core snapshot")}
    assert manifest["upload_validation"] == "size-only"
    assert "uploaded_at" in manifest and "verified_at" not in manifest
    del worker.env["FAKE_S5CMD_FORBID_ARCHIVE_CAT"]
    destination = tmp_path / "restored"
    worker.restore_checkpoint(uri, destination)
    assert (destination / "chainstate/state.dat").read_bytes() == b"a consistent Core snapshot"
    worker.run_id = "next-run"
    worker.backup(source, ["chainstate"], "s3://local/core", {"height": 550}, "checkpoint.tar.zst")
    assert sorted(path.name for path in (tmp_path / "s3/local/core").iterdir()) == ["checkpoint.tar.zst", "manifest.json"]
    assert json.loads((tmp_path / "s3/local/core/manifest.json").read_text())["height"] == 550
    # A failed upload leaves no valid backup until the next run.
    worker.env["FAKE_S5CMD_LS_SIZE_DELTA"] = "1"
    with pytest.raises(ValueError, match="size/key mismatch"):
        worker.backup(source, ["chainstate"], "s3://local/core", {"height": 600}, "checkpoint.tar.zst")
    assert not (tmp_path / "s3/local/core/manifest.json").exists()


def scanner_fixture(worker, height):
    scanner = worker.dumps / "Scanner"
    scanner.mkdir(parents=True, exist_ok=True)
    (scanner / "LastProcessedBlockHeight.txt").write_text(str(height))
    (scanner / "Tx0Candidates.txt").write_text("")
    (scanner / "Tx0CandidateMisses.txt").write_text("")
    (scanner / "Checkpoint.txt").write_text(f"{height}\nTx0Candidates.txt 0\nTx0CandidateMisses.txt 0\n")
    atomic_json(scanner / "ChainCheckpoint.json", {"height": height, "hash": "a" * 64})
    return scanner


def test_dumplings_copy_is_incremental_and_mirrors_deletions(worker, tmp_path):
    scanner = scanner_fixture(worker, 500)
    (scanner / "Obsolete.txt").write_text("gone next run")
    uri = worker.publish_dumplings(500)
    remote = tmp_path / "s3/local/dumps"
    assert uri == "s3://local/dumps/manifest.json"
    assert (remote / "Scanner/Obsolete.txt").exists()
    (scanner / "Obsolete.txt").unlink()
    scanner_fixture(worker, 600)
    worker.publish_dumplings(600)
    assert sorted(path.name for path in remote.iterdir()) == ["Scanner", "manifest.json"]
    assert not (remote / "Scanner/Obsolete.txt").exists()
    manifest = json.loads((remote / "manifest.json").read_text())
    assert manifest["last_processed_height"] == 600
    assert manifest["files"]["LastProcessedBlockHeight.txt"] == 3
    restored = tmp_path / "restored/Scanner"
    worker.restore_dumplings(uri, restored)
    assert (restored / "LastProcessedBlockHeight.txt").read_text() == "600"
    (remote / "Scanner/Checkpoint.txt").write_text("truncated")
    with pytest.raises(ValueError, match="does not match its manifest"):
        worker.restore_dumplings(uri, tmp_path / "restored-again/Scanner")
    assert not (tmp_path / "restored-again/Scanner").exists()


def test_a_dumplings_size_mismatch_publishes_no_manifest(worker, tmp_path, monkeypatch):
    scanner_fixture(worker, 500)
    worker.publish_dumplings(500)
    monkeypatch.setattr(worker, "remote_sizes", lambda prefix: {})
    with pytest.raises(ValueError, match="does not match Scanner"):
        worker.publish_dumplings(500)
    assert not (tmp_path / "s3/local/dumps/manifest.json").exists()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_restore_still_checks_stream_checksum(worker, tmp_path):
    source = tmp_path / "source"
    (source / "chainstate").mkdir(parents=True)
    (source / "chainstate/state.dat").write_text("snapshot")
    uri = worker.backup(source, ["chainstate"], "s3://local/core", {}, "checkpoint.tar.zst")
    manifest_path = tmp_path / "s3" / uri.removeprefix("s3://")
    manifest = json.loads(manifest_path.read_text())
    manifest["archive_sha256"] = "0" * 64
    atomic_json(manifest_path, manifest)
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="SHA-256/stream verification failed"):
        worker.restore_checkpoint(uri, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "metadata",
    [
        {"key": "s3://local/wrong", "size": 1},
        {"key": "s3://local/archive", "size": True},
        {"key": "s3://local/archive", "size": "1"},
        {"key": "s3://local/archive", "size": 2},
    ],
)
def test_backup_metadata_must_match_key_and_integer_size(worker, monkeypatch, metadata):
    monkeypatch.setattr(
        worker, "s3", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(metadata))
    )
    with pytest.raises(ValueError, match="size/key mismatch"):
        worker.verify_remote_size("s3://local/archive", 1)


HEIGHT, BLOCK_HASH = 500000, "a" * 64


def fake_components(worker, monkeypatch, events):
    """Core, Dumplings, archiver and parser stand-ins; S3 transport and backups stay real."""
    monkeypatch.setattr(worker, "container_command", lambda *args, **kwargs: list(args))

    def start_core(offline, dbcache_mb=None):
        events.append(("offline-core", dbcache_mb) if offline else "online-core")
        (worker.core_dir / "blocks").mkdir(parents=True, exist_ok=True)
        (worker.core_dir / "chainstate").mkdir(exist_ok=True)
        (worker.core_dir / "chainstate/state.dat").write_text("verified chainstate")
        (worker.core_dir / ".lock").touch()
        (worker.core_dir / ".cookie").write_text("local fixture")
        if not offline:
            for number in (0, 1):
                (worker.core_dir / f"blocks/blk{number:05d}.dat").write_bytes(bytes(88))
        return {"blocks": HEIGHT, "bestblockhash": BLOCK_HASH}

    def stop_core():
        events.append("clean-stop")
        (worker.core_dir / ".cookie").unlink()

    def rpc(method, *args):
        return {
            "getblockchaininfo": {
                "blocks": HEIGHT,
                "headers": HEIGHT,
                "bestblockhash": BLOCK_HASH,
                "verificationprogress": 1,
                "initialblockdownload": False,
            },
            "getnetworkinfo": {"connections": 1},
            "setnetworkactive": False,
            "getblockhash": BLOCK_HASH,
            "getblockcount": HEIGHT,
        }[method]

    def archive(prune=False, **kwargs):
        worker.archive_state.mkdir(exist_ok=True)
        (worker.archive_state / "verified.json").write_text("verified archive fixture")
        if prune:
            assert kwargs["number"] == 1
            (worker.core_dir / "blocks/blk00000.dat").unlink()
            events.append("prune")
        else:
            events.append("verified-block-archive")

    def run(command, name, poll=None):
        events.append(name)
        if name == "dumplings":
            scanner_fixture(worker, HEIGHT)
        elif name == "blocksci-config":
            atomic_json(worker.index / "config.json", {"parser": {"maxBlockNum": HEIGHT + 1}})
        elif name == "blocksci":
            atomic_json(
                worker.index / "parsed/parser/blockList.dat.resume.json",
                {"schema_version": 1, "file_number": 1, "data_offset": 8, "block_size": 80},
            )
            (worker.index / "parsed/chain").mkdir(exist_ok=True)
            (worker.index / "parsed/chain/block.dat").write_text("indexed fixture")
        elif name == "blocksci-verify":
            atomic_json(
                worker.work / "verified.json", {"exported_max_block": HEIGHT, "exported_block_hash": BLOCK_HASH}
            )

    for name, value in {
        "start_core": start_core,
        "stop_core": stop_core,
        "rpc": rpc,
        "archive": archive,
        "run_command": run,
    }.items():
        monkeypatch.setattr(worker, name, value)


def synchronize(worker, *stages):
    for stage in stages:
        worker.stage = stage
        (worker.restore_dataset if stage == "restore" else getattr(worker, stage))()
        assert not worker.active.exists(), f"{stage} must release the dataset"


def wipe_working_dataset(worker):
    shutil.rmtree(worker.root)
    shutil.rmtree(worker.index)


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_blocksci_job_completes_the_index_without_backing_it_up(worker, tmp_path, monkeypatch):
    events = []
    fake_components(worker, monkeypatch, events)
    synchronize(worker, "restore", "producer", "blocksci")
    assert not (worker.index / ".writing").exists()
    assert (worker.index / "files.txt").read_text() == mainnet_runtime.index_inventory(worker.index)
    progress = json.loads((worker.run / "blocksci.json").read_text())
    assert (progress["phase"], progress["state"]) == ("indexed", "done")
    assert not (tmp_path / "s3/local/index/manifest.json").exists()
    assert not (worker.run / "done.json").exists()
    # Core never starts in the BlockSci job.
    assert events[events.index("blocksci-config") :] == ["blocksci-config", "blocksci", "blocksci-verify"]


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_local_four_job_chain_backs_up_the_post_prune_core(worker, tmp_path, monkeypatch):
    """Real local backup transport; only Core, Dumplings and parser processes are mocked."""
    events = []
    fake_components(worker, monkeypatch, events)
    synchronize(worker, "restore")
    assert json.loads((worker.run / "restore.json").read_text())["action"] == "skipped-no-backup"
    synchronize(worker, "producer", "blocksci", "checkpoint")
    assert events.index("verified-block-archive") < events.index("blocksci") < events.index("prune")
    # The checkpoint job's offline Core gets a small cache; it only loads the chain to prune.
    assert ("offline-core", min(1024, worker.c["dbcache_mb"])) in events
    assert events[-1] == "clean-stop"
    done = json.loads((worker.run / "done.json").read_text())
    assert done["state"] == "done"
    storage = done["storage"]
    assert storage["multijob"]["core"]["bitcoin/chainstate"] == len("verified chainstate")
    assert storage["multijob"]["core"]["bitcoin/blocks"] == 88
    assert storage["multijob"]["index"]["parsed"] > 0
    for key, uri in (("core_checkpoint", done["core_checkpoint"]), ("index_backup", done["index_backup"])):
        manifest = json.loads((tmp_path / "s3" / uri.removeprefix("s3://")).read_text())
        assert storage["s3"][key] == manifest["archive_size"] > 0
    restored = tmp_path / "recovered-core"
    worker.restore_checkpoint(done["core_checkpoint"], restored)
    assert not (restored / "bitcoin/blocks/blk00000.dat").exists()
    assert (restored / "bitcoin/blocks/blk00001.dat").exists()
    assert (restored / "bitcoin/chainstate/state.dat").read_text() == "verified chainstate"
    # Scanner/ is not in the Core checkpoint; it comes back from its incremental copy.
    assert not (restored / "dumplings").exists()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_restore_job_refills_an_emptied_dataset_for_the_next_synchronization(worker, tmp_path, monkeypatch):
    events = []
    fake_components(worker, monkeypatch, events)
    synchronize(worker, "restore", "producer", "blocksci", "checkpoint")
    index_files = (worker.index / "files.txt").read_text()
    wipe_working_dataset(worker)
    worker.run_id = "mainnet-sync-local-2"
    synchronize(worker, "restore")
    restore = json.loads((worker.run / "restore.json").read_text())
    assert (restore["action"], restore["height"]) == ("restored", HEIGHT)
    assert (worker.core_dir / "chainstate/state.dat").read_text() == "verified chainstate"
    assert not (worker.core_dir / "blocks/blk00000.dat").exists()
    assert (worker.archive_state / "verified.json").is_file()
    assert (worker.dumps / "Scanner/LastProcessedBlockHeight.txt").read_text() == str(HEIGHT)
    assert (worker.index / "files.txt").read_text() == index_files
    assert not [*worker.root.glob(".restore-*"), *worker.index.glob(".restore-*")]
    # The restored copy is complete, so the producer continues incrementally.
    worker.stage = "producer"
    worker.require_complete_dataset()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_restore_job_leaves_a_dataset_that_is_not_empty(worker, monkeypatch):
    events = []
    fake_components(worker, monkeypatch, events)
    synchronize(worker, "restore", "producer", "blocksci", "checkpoint")
    shutil.rmtree(worker.index)
    monkeypatch.setattr(worker, "restore_checkpoint", lambda *args: pytest.fail("a partial dataset is never mixed"))
    synchronize(worker, "restore")
    assert json.loads((worker.run / "restore.json").read_text())["action"] == "skipped-not-empty"
    worker.stage = "producer"
    with pytest.raises(ValueError, match=r"incomplete \(missing: BlockSci index\)"):
        worker.require_complete_dataset()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
@pytest.mark.parametrize("damage", ["foreign-run", "missing-index-backup"])
def test_restore_job_refuses_backups_that_do_not_belong_together(worker, tmp_path, monkeypatch, damage):
    events = []
    fake_components(worker, monkeypatch, events)
    synchronize(worker, "restore", "producer", "blocksci", "checkpoint")
    wipe_working_dataset(worker)
    if damage == "foreign-run":
        manifest = tmp_path / "s3/local/dumps/manifest.json"
        atomic_json(manifest, {**json.loads(manifest.read_text()), "run_id": "another-run"})
        message = "do not belong together: run IDs differ"
    else:
        (tmp_path / "s3/local/index/manifest.json").unlink()
        message = "lacks the backups index"
    worker.stage = "restore"
    with pytest.raises(ValueError, match=message):
        worker.restore_dataset()
    assert worker.dataset_members() == []


def test_restore_job_refuses_archived_blocks_without_backups(worker, tmp_path):
    (tmp_path / "s3/local/blocks").mkdir(parents=True)
    (tmp_path / "s3/local/blocks/blk00000.dat").write_bytes(bytes(88))
    worker.stage = "restore"
    with pytest.raises(ValueError, match="archived blocks but no backups"):
        worker.restore_dataset()


def test_producer_refuses_an_empty_dataset_the_restore_job_did_not_check(worker):
    worker.stage = "producer"
    with pytest.raises(ValueError, match="restore job did not check S3"):
        worker.require_complete_dataset()
    atomic_json(worker.run / "restore.json", {"run_id": "an-earlier-run", "action": "skipped-no-backup"})
    with pytest.raises(ValueError, match="restore job did not check S3"):
        worker.require_complete_dataset()
    first_synchronization(worker)
    worker.require_complete_dataset()


def test_producer_continues_an_unfinished_first_synchronization(worker):
    (worker.core_dir / "blocks").mkdir(parents=True)
    (worker.core_dir / "blocks/blk00000.dat").write_bytes(bytes(88))
    worker.require_complete_dataset()
    # Without the full block history and without an index, nothing can be parsed any more.
    (worker.core_dir / "blocks/blk00000.dat").unlink()
    with pytest.raises(ValueError, match="incomplete"):
        worker.require_complete_dataset()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_checkpoint_job_refuses_an_index_another_job_changed(worker, tmp_path, monkeypatch):
    events = []
    fake_components(worker, monkeypatch, events)
    synchronize(worker, "restore", "producer", "blocksci")
    manifest = json.loads((worker.index / "manifest.json").read_text())
    atomic_json(worker.index / "manifest.json", {**manifest, "run_id": "a-manual-index-update"})
    worker.stage = "checkpoint"
    with pytest.raises(ValueError, match="not the one this synchronization"):
        worker.checkpoint()
    assert not (tmp_path / "s3/local/index").exists()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_failed_core_checkpoint_cannot_publish_done(worker, monkeypatch):
    events = []
    fake_components(worker, monkeypatch, events)
    synchronize(worker, "restore", "producer", "blocksci")
    published = []
    real_backup, real_publish = worker.backup, worker.publish_json

    def backup(root, members, prefix, metadata, name):
        if prefix == worker.c["backup_uri"]:
            events.append("core-backup-failed")
            raise RuntimeError("final checkpoint upload failed")
        events.append("index-backup")
        return real_backup(root, members, prefix, metadata, name)

    monkeypatch.setattr(worker, "backup", backup)
    monkeypatch.setattr(worker, "publish_json", lambda uri, value: published.append(uri) or real_publish(uri, value))
    worker.stage = "checkpoint"
    with pytest.raises(RuntimeError, match="final checkpoint"):
        worker.checkpoint()
    offline = ("offline-core", min(1024, worker.c["dbcache_mb"]))
    assert events[-5:] == ["index-backup", offline, "prune", "clean-stop", "core-backup-failed"]
    assert not (worker.run / "done.json").exists()
    assert not any(uri.endswith(("/done.json", "mainnet-checkpoint.done")) for uri in published)
    # The index stays complete: the next synchronization updates it and backs it up again.
    assert not (worker.index / ".writing").exists()
