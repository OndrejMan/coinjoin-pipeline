"""Configuration policy: which experiment files resolve and which rule each invalid one breaks."""

import re
from pathlib import Path

import pytest
import yaml

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.configuration import set_mapping_path
from coinjoin_pipeline.context import resolve_configuration

S3 = {
    "artifacts.uri": "s3://bucket/runs",
    "artifacts.endpoint_url": "https://s3.example.invalid",
    "artifacts.credentials_file": "/storage/user/.aws/credentials",
    "artifacts.profile": "coinjoin",
}
PBS_FROM_S3 = {"action": "pbs-from-s3", "run_id": "run-2", "engine": "wasabi", **S3}
S3_EMULATION = {
    "engine": "wasabi",
    "driver": "kubernetes",
    "artifacts.backend": "s3",
    "run_id": "run-1",
    "artifacts.secret_name": "coinjoin-s3",
    "kubernetes.reuse_namespace": True,
}
# The emulation options the S3 Job forwards to its controller.
EMULATION_OVERRIDES = {
    "kubernetes.distributor_startup_timeout": 1800,
    "kubernetes.btc_node_image": "btc-node:test",
    "kubernetes.btc_node_initial_block_count": 201,
    "kubernetes.image_pull_policy": "IfNotPresent",
    "kubernetes.owner": "xman",
    "kubernetes.planned_quota": '{"requests.cpu": "1.5"}',
}
# A mainnet analysis that only reads the published index of another run.
MAINNET_SCAN = str(Path(__file__).resolve().parents[2] / "scripts" / "metacentrum_mainnet_coinjoin_scan.py")
READ_ONLY_SCRIPT = {**PBS_FROM_S3, "stages.blocksci": True, "blocksci.task": "script", "blocksci.script": MAINNET_SCAN}
SOURCE = {"blocksci.cache_source_run_id": "mainnet-index"}
PINNED_RANGE = {"blocksci.pin_height": 850000, "blocksci.scan_start_height": 741213, "blocksci.scan_end_height": 850001}
PERSISTENT_UPDATE = {
    **PBS_FROM_S3,
    "stages.blocksci": True,
    "blocksci.workflow": "cached",
    "blocksci.task": "update",
    "blocksci.index_dir": "/scratch/multijob-brno/user/blocksci-mainnet",
    "blocksci.bitcoin_blocks_uri": "s3://blocks/mainnet",
    "blocksci.network": "bitcoin",
    "blocksci.max_block": 850100,
    "blocksci.expected_block_hash": "a" * 64,
}

VALID = {
    "analyze": {"action": "analyze", "engine": "wasabi", "run_dir": "run-1"},
    "reusable parse from a bitcoin-blocks URI": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "reusable",
        "blocksci.task": "parse",
        "blocksci.bitcoin_blocks_uri": "s3://blocks/mainnet",
        "blocksci.network": "bitcoin",
        "blocksci.max_block": 850100,
        "blocksci.expected_block_hash": "a" * 64,
    },
    "cached update": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "cached",
        "blocksci.task": "update",
        "blocksci.cache_source_run_id": "run-1",
        "blocksci.external_bitcoin_datadir": "/storage/external/bitcoin",
        "blocksci.network": "bitcoin",
        "blocksci.max_block": 850100,
    },
    "cached update from a bitcoin-blocks URI": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "cached",
        "blocksci.task": "update",
        "blocksci.cache_source_run_id": "run-1",
        "blocksci.bitcoin_blocks_uri": "s3://blocks/mainnet",
        "blocksci.network": "bitcoin",
        "blocksci.max_block": 850100,
        "blocksci.expected_block_hash": "a" * 64,
    },
    "read-only analysis over another run's cache": {
        **READ_ONLY_SCRIPT,
        "blocksci.workflow": "cached",
        **SOURCE,
        **PINNED_RANGE,
    },
    "persistent update on multijob scratch with an S3 backup": {
        **PERSISTENT_UPDATE,
        "pbs.blocksci.multijob": "brno",
        "blocksci.index_backup_uri": "s3://bucket/blocksci-index",
    },
    "index backup": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "cached",
        "blocksci.task": "backup",
        "blocksci.index_dir": "/scratch/multijob-brno/user/blocksci-mainnet",
        "pbs.blocksci.multijob": "brno",
        "blocksci.index_backup_uri": "s3://bucket/blocksci-index",
    },
    "index backup at a fixed state prefix": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "cached",
        "blocksci.task": "backup",
        "blocksci.index_dir": "/scratch/multijob-brno/user/blocksci-mainnet",
        "pbs.blocksci.multijob": "brno",
        "blocksci.index_backup_uri": "s3://bucket/blocksci-index",
        "blocksci.index_state_uri": "s3://bucket/bitcoin-mainnet/index",
    },
    "persistent script analysis": {
        **READ_ONLY_SCRIPT,
        "blocksci.workflow": "cached",
        "blocksci.index_dir": "/storage/brno2/home/user/blocksci-mainnet",
        **PINNED_RANGE,
    },
    "mainnet baseline from Dumplings": {
        **PBS_FROM_S3,
        "stages.analysis": True,
        "coinjoin_type": "joinmarket",
        "dumplings_uri": "s3://bucket/dumplings",
    },
    "S3 emulation": {"action": "emulate", **S3_EMULATION, **S3},
    "S3 full-run": {"action": "full-run", **S3_EMULATION, **S3, "stages.analysis": True, "stages.blocksci": True},
    "S3 emulation with its Job overrides": {"action": "emulate", **S3_EMULATION, **S3, **EMULATION_OVERRIDES},
}

INVALID = {
    "index state prefix for an analysis": {
        **VALID["persistent script analysis"],
        "blocksci.index_state_uri": "s3://bucket/bitcoin-mainnet/index",
    },
    "conflicting run selectors": {"action": "coinjoin-analysis", "run_dir": "run-1", "all_runs": True},
    "mainnet archive without a checkpoint": {
        key: value
        for key, value in VALID["reusable parse from a bitcoin-blocks URI"].items()
        if key != "blocksci.expected_block_hash"
    },
    "mainnet archive with a malformed checkpoint": {
        **VALID["reusable parse from a bitcoin-blocks URI"],
        "blocksci.expected_block_hash": "not-a-block-hash",
    },
    "mappings without its PBS stage": {"action": "mappings", "engine": "wasabi", "run_dir": "run-1"},
    "mappings with JoinMarket": {"action": "full-run", "engine": "joinmarket", "stages.mappings": True},
    "mappings with a JoinMarket CoinJoin type": {
        "action": "mappings",
        "engine": "wasabi",
        "coinjoin_type": "joinmarket",
        "run_dir": "run-1",
        "stages.mappings": True,
    },
    "unconfirmed clean": {"action": "clean"},
    "S3 emulation without frontend credentials": {
        "action": "emulate",
        **S3_EMULATION,
        "artifacts.uri": S3["artifacts.uri"],
        "artifacts.endpoint_url": S3["artifacts.endpoint_url"],
    },
    "S3 emulation with an empty artifact URI": {"action": "emulate", **S3_EMULATION, **S3, "artifacts.uri": ""},
    "S3 full-run in parallel": {
        "action": "full-run",
        **S3_EMULATION,
        **S3,
        "stages.analysis": True,
        "stages.blocksci": True,
        "parallel": True,
    },
    "reusable BlockSci on shared storage": {
        "action": "full-run",
        "engine": "wasabi",
        "stages.blocksci": True,
        "pbs.bitcoin_datadir": "/storage/btc",
        "blocksci.workflow": "reusable",
    },
    "update without a source cache": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "cached",
        "blocksci.task": "update",
        "blocksci.external_bitcoin_datadir": "/storage/external/bitcoin",
        "blocksci.network": "bitcoin",
        "blocksci.max_block": 850100,
    },
    "update without a Bitcoin source": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "cached",
        "blocksci.task": "update",
        "blocksci.cache_source_run_id": "run-1",
    },
    "two BlockSci sources": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "reusable",
        "blocksci.task": "parse",
        "blocksci.bitcoin_blocks_uri": "s3://blocks/mainnet",
        "blocksci.external_bitcoin_datadir": "/storage/external/bitcoin",
        "blocksci.network": "bitcoin",
        "blocksci.max_block": 850100,
    },
    "network without a source": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "reusable",
        "blocksci.task": "parse",
        "blocksci.network": "bitcoin",
    },
    "script task without a script": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "reusable",
        "blocksci.task": "script",
    },
    "external task without a baseline": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "reusable",
        "blocksci.task": "external",
    },
    "source cache for detect": {**PBS_FROM_S3, "stages.blocksci": True, "blocksci.workflow": "cached", **SOURCE},
    "source cache for an analysis without the cached workflow": {
        **READ_ONLY_SCRIPT,
        "blocksci.workflow": "reusable",
        **SOURCE,
    },
    "analysis reading its own run as the source": {
        **READ_ONLY_SCRIPT,
        "blocksci.workflow": "cached",
        "blocksci.cache_source_run_id": "run-2",
    },
    "pin height outside a script task": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "cached",
        "blocksci.task": "notebook",
        "blocksci.pin_height": 850000,
    },
    "empty scan range": {
        **READ_ONLY_SCRIPT,
        "blocksci.workflow": "cached",
        **PINNED_RANGE,
        "blocksci.scan_end_height": 741213,
    },
    "scan range past the pin": {
        **READ_ONLY_SCRIPT,
        "blocksci.workflow": "cached",
        **PINNED_RANGE,
        "blocksci.scan_end_height": 850002,
    },
    "multijob index without the multijob resource": PERSISTENT_UPDATE,
    "multijob resource of another location": {**PERSISTENT_UPDATE, "pbs.blocksci.multijob": "plzen"},
    "multijob for the coinjoin-analysis stage": {
        **PBS_FROM_S3,
        "stages.analysis": True,
        "pbs.analysis.multijob": "brno",
    },
    "index outside shared storage": {**PERSISTENT_UPDATE, "blocksci.index_dir": "/tmp/blocksci-mainnet"},
    "both a persistent index and a source run": {**PERSISTENT_UPDATE, "pbs.blocksci.multijob": "brno", **SOURCE},
    "index backup outside S3": {
        **PERSISTENT_UPDATE,
        "pbs.blocksci.multijob": "brno",
        "blocksci.index_backup_uri": "/storage/brno2/home/user/copy",
    },
    "backup task without a backup prefix": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "cached",
        "blocksci.task": "backup",
        "blocksci.index_dir": "/storage/brno2/home/user/blocksci-mainnet",
    },
    "persistent index for detect": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "blocksci.workflow": "cached",
        "blocksci.index_dir": "/storage/brno2/home/user/blocksci-mainnet",
    },
    "false positives outside the external task": {
        **READ_ONLY_SCRIPT,
        "blocksci.workflow": "cached",
        **SOURCE,
        "blocksci.external_false_cjtxs_uri": "s3://bucket/dumplings/false_cjtxs.json",
    },
    "Dumplings baseline without its PBS stage": {
        **PBS_FROM_S3,
        "stages.blocksci": True,
        "dumplings_uri": "s3://bucket/dumplings",
    },
    "S3 Job overrides on a Kubernetes run without S3": {
        "action": "full-run",
        "engine": "wasabi",
        "driver": "kubernetes",
        "kubernetes.btc_node_image": "btc-node:test",
    },
    "report resources without a report job": {**PBS_FROM_S3, "stages.analysis": True, "pbs.unified_report.ncpus": 2},
}


def experiment_file(values, tmp_path):
    """The experiment as a YAML file, loaded the way `cjp run` loads it."""
    data = {"action": "full-run"}
    for path, value in values.items():
        set_mapping_path(data, path, value)
    target = tmp_path / "experiment.yaml"
    target.write_text(yaml.safe_dump(data))
    return load_configuration(["run", str(target)])


@pytest.mark.parametrize("values", VALID.values(), ids=VALID.keys())
def test_a_valid_experiment_resolves(values, tmp_path):
    assert resolve_configuration(experiment_file(values, tmp_path)).action == values.get("action", "full-run")


@pytest.mark.parametrize("values", INVALID.values(), ids=INVALID.keys())
def test_an_invalid_experiment_is_refused(values, tmp_path):
    with pytest.raises(ValueError):
        resolve_configuration(experiment_file(values, tmp_path))


@pytest.mark.parametrize(
    "case,message",
    [
        ("source cache for detect", "requires blocksci.task update, script, external, or notebook"),
        ("source cache for an analysis without the cached workflow", "requires blocksci.workflow cached"),
        ("analysis reading its own run as the source", "must differ from target run_id"),
        ("pin height outside a script task", "blocksci.pin_height requires blocksci.task script"),
        ("empty scan range", "must be below blocksci.scan_end_height"),
        ("scan range past the pin", "must not exceed blocksci.pin_height + 1"),
        ("multijob index without the multijob resource", "requires pbs.blocksci.multijob brno"),
        ("multijob resource of another location", "requires pbs.blocksci.multijob brno"),
        ("multijob for the coinjoin-analysis stage", "pbs.analysis.multijob is supported only for the BlockSci"),
        ("index outside shared storage", "must be under /scratch/multijob-<location>/ or /storage/"),
        ("both a persistent index and a source run", "choose one index source"),
        ("index backup outside S3", "artifact URI must use s3://"),
        ("backup task without a backup prefix", "blocksci.task backup requires blocksci.index_backup_uri"),
        ("persistent index for detect", "blocksci.index_dir requires blocksci.task parse"),
        ("index state prefix for an analysis", "blocksci.index_state_uri requires blocksci.index_dir"),
        (
            "false positives outside the external task",
            "blocksci.external_false_cjtxs_uri requires blocksci.task external",
        ),
        ("Dumplings baseline without its PBS stage", "dumplings_uri requires pbs-from-s3 stages.analysis"),
    ],
)
def test_read_only_analysis_rules_name_the_violation(case, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        resolve_configuration(PipelineConfiguration.from_paths(INVALID[case]))
