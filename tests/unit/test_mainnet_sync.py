from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import os
import shutil

import pytest

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.context import resolve_configuration
from coinjoin_pipeline.execution import mainnet_sync
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
                    "uri": "s3://local/runs",
                    "endpoint_url": "http://127.0.0.1:9000",
                    "credentials_file": str(tmp_path / "credentials"),
                    "profile": "local",
                },
                "mainnet": {
                    "root": str(tmp_path / "core"),
                    "backup_uri": "s3://local/core",
                    "blocks_uri": "s3://local/blocks",
                    "dumplings_uri": "s3://local/dumps",
                },
                "blocksci": {"index_dir": str(tmp_path / "index"), "index_backup_uri": "s3://local/index"},
            }
        )
    )


def test_two_scripts_are_self_contained_and_syntactically_valid(config):
    for stage in ("producer", "blocksci"):
        script = mainnet_sync.render_sync(config, stage)
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        assert "def scanner_complete" in script
        assert "rm -rf" not in script if stage == "producer" else "INDEX_LOCK_WAIT=0" in script
        assert "clean_scratch" not in script
    assert not config.stages.blocksci


def test_dependencies_are_persisted_and_rolled_back(config, monkeypatch):
    calls, cancelled = [], []
    monkeypatch.setattr(mainnet_sync, "ensure_empty_run_prefix", lambda *_: None)

    def submit(job, _dry):
        calls.append(job)
        if len(calls) == 2:
            raise RuntimeError("scheduler refused second job")
        return "101.local"

    monkeypatch.setattr(mainnet_sync, "submit_pbs_job", submit)
    monkeypatch.setattr(mainnet_sync, "qdel_pbs_job", lambda job: cancelled.append(job) or True)
    with pytest.raises(RuntimeError, match="scheduler refused"):
        mainnet_sync.submit_sync(config)
    assert calls[1].dependencies == ("101.local",)
    assert cancelled == ["101.local"]
    assert (config.runs_path / config.run_id / ".pbs/mainnet-producer.jobid").read_text().strip() == "101.local"


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
            artifact_uri=config.artifacts.uri,
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


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_backup_has_no_readback_and_size_failure_preserves_previous_pointer(worker, tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "chainstate").mkdir(parents=True)
    (source / "chainstate/state.dat").write_bytes(b"a consistent Core snapshot")
    worker.env["FAKE_S5CMD_FORBID_ARCHIVE_CAT"] = "1"
    monkeypatch.setattr(worker, "remote_hash", lambda _: pytest.fail("archive backup must not read back content"))
    uri = worker.backup(source, ["chainstate"], "s3://local/core", {"height": 500})
    manifest = json.loads((tmp_path / "s3" / uri.removeprefix("s3://")).read_text())
    archive = tmp_path / "s3" / manifest["archive_uri"].removeprefix("s3://")
    assert manifest["archive_size"] == archive.stat().st_size
    assert manifest["upload_validation"] == "size-only"
    assert "uploaded_at" in manifest and "verified_at" not in manifest
    del worker.env["FAKE_S5CMD_FORBID_ARCHIVE_CAT"]
    destination = tmp_path / "restored"
    worker.restore_checkpoint(uri, destination)
    assert (destination / "chainstate/state.dat").read_bytes() == b"a consistent Core snapshot"
    pointer = tmp_path / "s3/local/core/latest.json"
    old = pointer.read_bytes()
    worker.run_id = "new-generation"
    worker.env["FAKE_S5CMD_HEAD_SIZE_DELTA"] = "1"
    with pytest.raises(ValueError, match="size/key mismatch"):
        worker.backup(source, ["chainstate"], "s3://local/core", {"height": 600})
    assert pointer.read_bytes() == old
    assert not (tmp_path / "s3/local/core/generations/new-generation/manifest.json").exists()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_restore_still_checks_stream_checksum(worker, tmp_path):
    source = tmp_path / "source"
    (source / "chainstate").mkdir(parents=True)
    (source / "chainstate/state.dat").write_text("snapshot")
    uri = worker.backup(source, ["chainstate"], "s3://local/core", {})
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


def test_final_core_checkpoint_failure_cannot_publish_done(worker, monkeypatch):
    ready = {
        "run_id": worker.run_id,
        "state": "ready",
        "height": 500,
        "hash": "a" * 64,
        "dumplings_height": 500,
        "dumplings_backup": "s3://local/dumps",
        "source_dir": str(worker.core_dir),
    }
    atomic_json(worker.run / "ready.json", ready)
    atomic_json(worker.root / "current-ready.json", ready)
    (worker.core_dir / "blocks").mkdir(parents=True)
    (worker.core_dir / "blocks/blk00000.dat").write_bytes(bytes(88))
    (worker.core_dir / ".lock").touch()
    published, steps = [], []
    monkeypatch.setattr(worker, "progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "publish_json", lambda uri, value: published.append(uri))
    monkeypatch.setattr(worker, "container_command", lambda *args, **kwargs: list(args))

    def run(command, name, poll=None):
        if name == "blocksci-config":
            atomic_json(worker.index / "config.json", {"parser": {"maxBlockNum": 501}})
        if name == "blocksci":
            atomic_json(
                worker.index / "parsed/parser/blockList.dat.resume.json",
                {"schema_version": 1, "file_number": 0, "data_offset": 8, "block_size": 80},
            )
        if name == "blocksci-verify":
            atomic_json(worker.work / "verified.json", {"exported_max_block": 500, "exported_block_hash": "a" * 64})
        steps.append(name)

    monkeypatch.setattr(worker, "run_command", run)

    def backup(root, members, prefix, metadata):
        if prefix == worker.c["backup_uri"]:
            steps.append("core-backup-failed")
            raise RuntimeError("final checkpoint upload failed")
        steps.append("index-backup-verified")
        return "s3://local/index/generations/test/manifest.json"

    monkeypatch.setattr(worker, "backup", backup)

    def start(offline):
        assert offline
        steps.append("offline-core")
        return {"blocks": 500, "bestblockhash": "a" * 64}

    monkeypatch.setattr(worker, "start_core", start)
    monkeypatch.setattr(worker, "archive", lambda **kwargs: steps.append("prune"))
    monkeypatch.setattr(worker, "stop_core", lambda: steps.append("clean-stop"))
    with pytest.raises(RuntimeError, match="final checkpoint"):
        worker.blocksci()
    assert steps[-5:] == ["index-backup-verified", "offline-core", "prune", "clean-stop", "core-backup-failed"]
    assert not (worker.run / "done.json").exists()
    assert not any(uri.endswith("/done.json") or uri.endswith("mainnet-blocksci.done") for uri in published)
    assert worker.index.exists()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires zstd")
def test_local_two_job_handoff_backs_up_the_post_prune_core(worker, tmp_path, monkeypatch):
    """Real local backup transport; only Core, Dumplings and parser processes are mocked."""
    height, block_hash = 500000, "a" * 64
    events = []
    monkeypatch.setattr(worker, "container_command", lambda *args, **kwargs: list(args))

    def start_core(offline):
        events.append("offline-core" if offline else "online-core")
        (worker.core_dir / "blocks").mkdir(parents=True, exist_ok=True)
        (worker.core_dir / "chainstate").mkdir(exist_ok=True)
        (worker.core_dir / "chainstate/state.dat").write_text("verified chainstate")
        (worker.core_dir / ".lock").touch()
        (worker.core_dir / ".cookie").write_text("local fixture")
        for number in (0, 1):
            (worker.core_dir / f"blocks/blk{number:05d}.dat").write_bytes(bytes(88))
        return {"blocks": height, "bestblockhash": block_hash}

    monkeypatch.setattr(worker, "start_core", start_core)

    def stop_core():
        events.append("clean-stop")
        (worker.core_dir / ".cookie").unlink()

    monkeypatch.setattr(worker, "stop_core", stop_core)

    def rpc(method, *args):
        return {
            "getblockchaininfo": {
                "blocks": height,
                "headers": height,
                "bestblockhash": block_hash,
                "verificationprogress": 1,
                "initialblockdownload": False,
            },
            "getnetworkinfo": {"connections": 1},
            "setnetworkactive": False,
            "getblockhash": block_hash,
            "getblockcount": height,
        }[method]

    monkeypatch.setattr(worker, "rpc", rpc)

    def archive(prune=False, **kwargs):
        worker.archive_state.mkdir(exist_ok=True)
        (worker.archive_state / "verified.json").write_text("verified archive fixture")
        if prune:
            assert kwargs["number"] == 1
            assert (tmp_path / "s3/local/index/latest.json").exists()
            (worker.core_dir / "blocks/blk00000.dat").unlink()
            events.append("prune")
        else:
            events.append("verified-block-archive")

    monkeypatch.setattr(worker, "archive", archive)

    def run(command, name, poll=None):
        events.append(name)
        if name == "dumplings":
            scanner = worker.dumps / "Scanner"
            scanner.mkdir(exist_ok=True)
            (scanner / "LastProcessedBlockHeight.txt").write_text(str(height))
            (scanner / "Tx0Candidates.txt").write_text("")
            (scanner / "Tx0CandidateMisses.txt").write_text("")
            (scanner / "Checkpoint.txt").write_text(f"{height}\nTx0Candidates.txt 0\nTx0CandidateMisses.txt 0\n")
        elif name == "blocksci-config":
            atomic_json(worker.index / "config.json", {"parser": {"maxBlockNum": height + 1}})
        elif name == "blocksci":
            atomic_json(
                worker.index / "parsed/parser/blockList.dat.resume.json",
                {"schema_version": 1, "file_number": 1, "data_offset": 8, "block_size": 80},
            )
            (worker.index / "parsed/chain").mkdir()
            (worker.index / "parsed/chain/block.dat").write_text("indexed fixture")
        elif name == "blocksci-verify":
            atomic_json(
                worker.work / "verified.json", {"exported_max_block": height, "exported_block_hash": block_hash}
            )

    monkeypatch.setattr(worker, "run_command", run)
    worker.producer()
    assert not worker.active.exists()
    assert not (tmp_path / "s3/local/core/latest.json").exists()
    assert not (worker.run / "done.json").exists()
    worker.blocksci()
    assert not worker.active.exists()
    assert events.index("verified-block-archive") < events.index("blocksci") < events.index("prune")
    assert events[-1] == "clean-stop"
    done = json.loads((worker.run / "done.json").read_text())
    assert done["state"] == "done"
    restored = tmp_path / "recovered-core"
    worker.restore_checkpoint(done["core_checkpoint"], restored)
    assert not (restored / "bitcoin/blocks/blk00000.dat").exists()
    assert (restored / "bitcoin/blocks/blk00001.dat").exists()
    assert (restored / "bitcoin/chainstate/state.dat").read_text() == "verified chainstate"
    assert (restored / "dumplings/Scanner/Tx0Candidates.txt").exists()
