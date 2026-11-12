"""Each rule of a BlockSci index job is reported by the configuration, by its own message.

The job renderers repeat several of these checks. They run after the configuration was
validated, so these are the errors a user sees; every case names the rule it expects,
because a configuration that is merely invalid can fail for an unrelated reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.context import resolve_configuration

SCRIPT = str(Path(__file__).resolve().parents[2] / "scripts" / "blocksci_script_smoke.py")
INDEX = "/scratch/multijob-brno/user/blocksci-mainnet"
BACKUP = "s3://bucket/mainnet/blocksci-index"
BLOCKS = "s3://bucket/mainnet/blocks"
HASH = "a" * 64
MULTIJOB = {"blocksci": {"multijob": "brno"}}


def job(pbs: dict | None = None, run_id: str = "run-2", **blocksci: object) -> dict:
    """A BlockSci-only pbs-from-s3 job; the cases below change one thing of a valid one."""
    return {
        "action": "pbs-from-s3",
        "run_id": run_id,
        "artifacts": {
            "backend": "s3",
            "uri": "s3://bucket/runs",
            "endpoint_url": "https://s3.example",
            "credentials_file": "/storage/home/user/.aws/credentials",
            "profile": "test",
        },
        "stages": {"blocksci": True},
        "blocksci": {"workflow": "cached", **blocksci},
        **({"pbs": pbs} if pbs else {}),
    }


def persistent(task: str, **blocksci: object) -> dict:
    return job(MULTIJOB, task=task, index_dir=INDEX, **blocksci)


ARCHIVE = {"bitcoin_blocks_uri": BLOCKS, "network": "bitcoin", "max_block": 900000, "expected_block_hash": HASH}
REGTEST_ARCHIVE = {"bitcoin_blocks_uri": BLOCKS, "network": "bitcoin_regtest", "max_block": 300}

VALID = {
    "persistent parse": persistent("parse", workflow="reusable", **ARCHIVE),
    "persistent update": persistent("update", index_backup_uri=BACKUP, **ARCHIVE),
    "persistent backup": persistent("backup", index_backup_uri=BACKUP),
    "notebook over the index in place": persistent(
        "notebook", dumplings_dir="/scratch/multijob-brno/user/dumplings/Scanner"
    ),
    "notebook with the index in its scratch": persistent(
        "notebook", index_backup_uri=BACKUP, index_in_scratch=True, dumplings_uri="s3://bucket/dumplings"
    ),
    "notebook restoring a kept index": persistent(
        "notebook", index_backup_uri=BACKUP, restore_index=True, storage_report_uri="s3://bucket/report.json"
    ),
    "script over another run's cache": job(task="script", script=SCRIPT, cache_source_run_id="run-1"),
    "update of another run's cache": job(task="update", cache_source_run_id="run-1", **REGTEST_ARCHIVE),
    "external report": job(
        task="external",
        cache_source_run_id="run-1",
        external_baseline_uri="s3://bucket/dumplings/coinjoin_tx_info.json",
        external_false_cjtxs_uri="s3://bucket/dumplings/false_cjtxs.json",
    ),
}

INVALID = {
    # Where a persistent index may live, and which jobs may use one.
    "an index outside shared storage": (
        job(MULTIJOB, task="backup", index_dir="/home/user/index", index_backup_uri=BACKUP),
        "--blocksci-index-dir must be under /scratch/multijob-<location>/ or /storage/",
    ),
    "an index path that climbs out": (
        job(MULTIJOB, task="backup", index_dir=INDEX + "/../other", index_backup_uri=BACKUP),
        "--blocksci-index-dir must not contain '..'",
    ),
    "an index on a multijob the job does not request": (
        job(task="backup", index_dir=INDEX, index_backup_uri=BACKUP),
        "--blocksci-index-dir on multijob-brno requires --pbs-blocksci-multijob brno",
    ),
    "a multijob without an index on it": (
        job(MULTIJOB, task="backup", index_dir="/storage/home/user/index", index_backup_uri=BACKUP),
        "--pbs-blocksci-multijob brno requires --blocksci-index-dir under /scratch/multijob-brno/",
    ),
    "a multijob without any index": (
        job(MULTIJOB, task="script", script=SCRIPT, cache_source_run_id="run-1"),
        "--pbs-blocksci-multijob requires --blocksci-index-dir on that multijob scratch",
    ),
    "a backup prefix without an index": (
        job(task="script", script=SCRIPT, cache_source_run_id="run-1", index_backup_uri=BACKUP),
        "--blocksci-index-backup-uri requires --blocksci-index-dir",
    ),
    "a backup prefix that is not an S3 URI": (
        persistent("backup", index_backup_uri="/storage/home/user/copy"),
        "artifact URI must use s3:// and include a bucket",
    ),
    **{
        f"{option} without an index": (
            job(task="notebook", cache_source_run_id="run-1", **{option: value}),
            f"{flag} requires --blocksci-index-dir",
        )
        for option, value, flag in (
            ("index_in_scratch", True, "--blocksci-index-in-scratch"),
            ("restore_index", True, "--blocksci-restore-index"),
            ("dumplings_uri", "s3://bucket/dumplings", "--blocksci-dumplings-uri"),
            ("dumplings_dir", "/storage/home/user/Scanner", "--blocksci-dumplings-dir"),
            ("storage_report_uri", "s3://bucket/report.json", "--blocksci-storage-report-uri"),
        )
    },
    "detection over a persistent index": (
        persistent("detect"),
        "--blocksci-index-dir requires --blocksci-task parse, update, backup, script, external, or notebook",
    ),
    "a persistent index and another run's cache": (
        persistent("script", script=SCRIPT, cache_source_run_id="run-1"),
        "choose one index source: --blocksci-index-dir or --blocksci-cache-source-run-id",
    ),
    "a persistent parse without a block source": (
        persistent("parse", workflow="reusable"),
        "a persistent index is parsed from --blocksci-bitcoin-blocks-uri or --blocksci-external-bitcoin-datadir",
    ),
    "a backup job without a backup prefix": (
        persistent("backup"),
        "--blocksci-task backup requires --blocksci-index-backup-uri",
    ),
    # How a reading job gets its copy of the index and of Dumplings.
    "an index in scratch without a backup": (
        persistent("notebook", index_in_scratch=True),
        "--blocksci-index-in-scratch requires --blocksci-index-backup-uri and --blocksci-task",
    ),
    "a restored index for a script": (
        persistent("script", script=SCRIPT, index_backup_uri=BACKUP, restore_index=True),
        "--blocksci-restore-index requires --blocksci-index-backup-uri and --blocksci-task notebook",
    ),
    "a storage report without a kept copy": (
        persistent("notebook", storage_report_uri="s3://bucket/report.json"),
        "--blocksci-storage-report-uri requires --blocksci-restore-index",
    ),
    "a Dumplings copy for an index read in place": (
        persistent("notebook", dumplings_uri="s3://bucket/dumplings"),
        "--blocksci-dumplings-uri requires --blocksci-index-in-scratch or --blocksci-restore-index",
    ),
    "a Dumplings directory for an index in scratch": (
        persistent("notebook", index_backup_uri=BACKUP, index_in_scratch=True, dumplings_dir="/storage/Scanner"),
        "--blocksci-dumplings-dir requires --blocksci-task notebook and --blocksci-index-dir",
    ),
    "a Dumplings directory outside shared storage": (
        persistent("notebook", dumplings_dir="/tmp/Scanner"),
        "--blocksci-dumplings-dir must be under /scratch/multijob-<location>/ or /storage/, without '..'",
    ),
    "a Dumplings directory that climbs out": (
        persistent("notebook", dumplings_dir="/scratch/multijob-brno/user/../other/Scanner"),
        "--blocksci-dumplings-dir must be under /scratch/multijob-<location>/ or /storage/, without '..'",
    ),
    "an index on an unknown multijob": (
        job(MULTIJOB, task="backup", index_dir="/scratch/multijob-ostrava/user/index", index_backup_uri=BACKUP),
        "--blocksci-index-dir must be under /scratch/multijob-<location>/ or /storage/",
    ),
    # The block source of a parse or update.
    "a mainnet archive without its block hash": (
        persistent("parse", workflow="reusable", **{**ARCHIVE, "expected_block_hash": None}),
        "Mainnet block archives require --blocksci-expected-block-hash at --blocksci-max-block",
    ),
    "a malformed block hash": (
        persistent("parse", workflow="reusable", **{**ARCHIVE, "expected_block_hash": "not-a-hash"}),
        "--blocksci-expected-block-hash must be 64 lowercase hexadecimal characters",
    ),
    "two block sources": (
        persistent("parse", workflow="reusable", external_bitcoin_datadir="/storage/bitcoin", **ARCHIVE),
        "choose only one BlockSci source",
    ),
    "a block source without its chain": (
        persistent("parse", workflow="reusable", bitcoin_blocks_uri=BLOCKS),
        "an external Bitcoin source requires --blocksci-network and --blocksci-max-block",
    ),
    "an update without a block source": (
        job(task="update", cache_source_run_id="run-1"),
        "--blocksci-task update requires --blocksci-external-bitcoin-datadir or --blocksci-bitcoin-blocks-uri",
    ),
    "an update without an index": (
        job(task="update", **REGTEST_ARCHIVE),
        "--blocksci-task update requires --blocksci-cache-source-run-id or --blocksci-index-dir",
    ),
    # Reading the cache of another run.
    "an update into its own source run": (
        job(task="update", cache_source_run_id="run-2", **REGTEST_ARCHIVE),
        "--blocksci-cache-source-run-id must differ from target --run-id",
    ),
    "a script reading its own run as a source": (
        job(task="script", script=SCRIPT, cache_source_run_id="run-2"),
        "--blocksci-cache-source-run-id must differ from target --run-id",
    ),
    "detection over another run's cache": (
        job(task="detect", cache_source_run_id="run-1"),
        "--blocksci-cache-source-run-id requires --blocksci-task update, script, external, or notebook",
    ),
    "a malformed source run ID": (
        job(task="script", script=SCRIPT, cache_source_run_id="bad/run"),
        "run ID must be at most 63 characters",
    ),
    # What each analysis task needs.
    "a script task without a script": (
        job(task="script", cache_source_run_id="run-1"),
        "--blocksci-task script requires --blocksci-script",
    ),
    "a script that does not exist": (
        job(task="script", script="/storage/home/user/missing.py", cache_source_run_id="run-1"),
        "BlockSci script not found: /storage/home/user/missing.py",
    ),
    "an external report without a baseline": (
        job(task="external", cache_source_run_id="run-1"),
        "--blocksci-task external requires --external-baseline-uri",
    ),
    "a false-positive list for a script": (
        job(
            task="script",
            script=SCRIPT,
            cache_source_run_id="run-1",
            external_false_cjtxs_uri="s3://bucket/false_cjtxs.json",
        ),
        "--external-false-cjtxs-uri requires --blocksci-task external",
    ),
    "a privileged notebook port": (
        persistent("notebook", notebook_port=80),
        "--blocksci-notebook-port must be between 1024 and 65535",
    ),
}


@pytest.mark.parametrize("name", VALID)
def test_a_supported_index_job_is_accepted(name):
    resolve_configuration(PipelineConfiguration.from_mapping(VALID[name]))


@pytest.mark.parametrize("name", INVALID)
def test_an_unsupported_index_job_names_its_rule(name):
    mapping, message = INVALID[name]

    with pytest.raises(ValueError, match=re.escape(message)):
        resolve_configuration(PipelineConfiguration.from_mapping(mapping))
