"""Equivalent configuration policy for YAML and direct command-line values."""

import re
from pathlib import Path

import pytest
import yaml

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.configuration import config_value, set_mapping_path
from coinjoin_pipeline.context import resolve_configuration

S3 = [
    "--artifact-uri",
    "s3://bucket/runs",
    "--s3-endpoint-url",
    "https://s3.example.invalid",
    "--s3-credentials-file",
    "/storage/user/.aws/credentials",
    "--s3-profile",
    "coinjoin",
]
PBS_FROM_S3 = ["pbs-from-s3", "--run-id", "run-2", "--engine", "wasabi", *S3]
S3_EMULATION = [
    "--engine",
    "wasabi",
    "--driver",
    "kubernetes",
    "--artifact-backend",
    "s3",
    "--run-id",
    "run-1",
    "--s3-secret-name",
    "coinjoin-s3",
    "--reuse-namespace",
]
# A mainnet analysis that only reads the published index of another run.
MAINNET_SCAN = str(Path(__file__).resolve().parents[2] / "scripts" / "metacentrum_mainnet_coinjoin_scan.py")
READ_ONLY_SCRIPT = [*PBS_FROM_S3, "--blocksciPbs", "--blocksci-task", "script", "--blocksci-script", MAINNET_SCAN]
SOURCE = ["--blocksci-cache-source-run-id", "mainnet-index"]
PINNED_RANGE = [
    "--blocksci-pin-height",
    "850000",
    "--blocksci-scan-start-height",
    "741213",
    "--blocksci-scan-end-height",
    "850001",
]
PERSISTENT_UPDATE = [
    *PBS_FROM_S3,
    "--blocksciPbs",
    "--blocksci-workflow",
    "cached",
    "--blocksci-task",
    "update",
    "--blocksci-index-dir",
    "/scratch/multijob-brno/user/blocksci-mainnet",
    "--blocksci-bitcoin-blocks-uri",
    "s3://blocks/mainnet",
    "--blocksci-network",
    "bitcoin",
    "--blocksci-max-block",
    "850100",
    "--blocksci-expected-block-hash",
    "a" * 64,
]

VALID = {
    "analyze": ["analyze", "--engine", "wasabi", "--run-dir", "run-1"],
    "reusable parse from a bitcoin-blocks URI": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "reusable",
        "--blocksci-task",
        "parse",
        "--blocksci-bitcoin-blocks-uri",
        "s3://blocks/mainnet",
        "--blocksci-network",
        "bitcoin",
        "--blocksci-max-block",
        "850100",
        "--blocksci-expected-block-hash",
        "a" * 64,
    ],
    "cached update": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "cached",
        "--blocksci-task",
        "update",
        "--blocksci-cache-source-run-id",
        "run-1",
        "--blocksci-external-bitcoin-datadir",
        "/storage/external/bitcoin",
        "--blocksci-network",
        "bitcoin",
        "--blocksci-max-block",
        "850100",
    ],
    "cached update from a bitcoin-blocks URI": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "cached",
        "--blocksci-task",
        "update",
        "--blocksci-cache-source-run-id",
        "run-1",
        "--blocksci-bitcoin-blocks-uri",
        "s3://blocks/mainnet",
        "--blocksci-network",
        "bitcoin",
        "--blocksci-max-block",
        "850100",
        "--blocksci-expected-block-hash",
        "a" * 64,
    ],
    "read-only analysis over another run's cache": [
        *READ_ONLY_SCRIPT,
        "--blocksci-workflow",
        "cached",
        *SOURCE,
        *PINNED_RANGE,
    ],
    "persistent update on multijob scratch with an S3 backup": [
        *PERSISTENT_UPDATE,
        "--pbs-blocksci-multijob",
        "brno",
        "--blocksci-index-backup-uri",
        "s3://bucket/blocksci-index",
    ],
    "index backup": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "cached",
        "--blocksci-task",
        "backup",
        "--blocksci-index-dir",
        "/scratch/multijob-brno/user/blocksci-mainnet",
        "--pbs-blocksci-multijob",
        "brno",
        "--blocksci-index-backup-uri",
        "s3://bucket/blocksci-index",
    ],
    "persistent script analysis": [
        *READ_ONLY_SCRIPT,
        "--blocksci-workflow",
        "cached",
        "--blocksci-index-dir",
        "/storage/brno2/home/user/blocksci-mainnet",
        *PINNED_RANGE,
    ],
    "mainnet baseline from Dumplings": [
        *PBS_FROM_S3,
        "--analysisPbs",
        "--coinjoin-type",
        "joinmarket",
        "--dumplings-uri",
        "s3://bucket/dumplings",
    ],
    "S3 emulation": ["emulate", *S3_EMULATION, *S3],
    "S3 full-run": ["full-run", *S3_EMULATION, *S3, "--analysisPbs", "--blocksciPbs"],
}

INVALID = {
    "conflicting run selectors": ["coinjoin-analysis", "--run-dir", "run-1", "--all-runs"],
    "mainnet archive without a checkpoint": VALID["reusable parse from a bitcoin-blocks URI"][:-2],
    "mainnet archive with a malformed checkpoint": [
        *VALID["reusable parse from a bitcoin-blocks URI"][:-1],
        "not-a-block-hash",
    ],
    "mappings without its PBS stage": [
        "mappings",
        "--engine",
        "wasabi",
        "--run-dir",
        "run-1",
    ],
    "mappings with JoinMarket": ["full-run", "--engine", "joinmarket", "--mappingsPbs"],
    "mappings with a JoinMarket CoinJoin type": [
        "mappings",
        "--engine",
        "wasabi",
        "--coinjoin-type",
        "joinmarket",
        "--run-dir",
        "run-1",
        "--mappingsPbs",
    ],
    "unconfirmed clean": ["clean"],
    "S3 emulation without frontend credentials": ["emulate", *S3_EMULATION, *S3[:4]],
    "S3 emulation with an empty artifact URI": [
        "emulate",
        *S3_EMULATION,
        "--artifact-uri",
        "",
        *S3[2:],
    ],
    "S3 full-run in parallel": [
        "full-run",
        *S3_EMULATION,
        *S3,
        "--analysisPbs",
        "--blocksciPbs",
        "--parallel",
    ],
    "reusable BlockSci on shared storage": [
        "full-run",
        "--engine",
        "wasabi",
        "--blocksciPbs",
        "--pbs-bitcoin-datadir",
        "/storage/btc",
        "--blocksci-workflow",
        "reusable",
    ],
    "update without a source cache": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "cached",
        "--blocksci-task",
        "update",
        "--blocksci-external-bitcoin-datadir",
        "/storage/external/bitcoin",
        "--blocksci-network",
        "bitcoin",
        "--blocksci-max-block",
        "850100",
    ],
    "update without a Bitcoin source": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "cached",
        "--blocksci-task",
        "update",
        "--blocksci-cache-source-run-id",
        "run-1",
    ],
    "two BlockSci sources": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "reusable",
        "--blocksci-task",
        "parse",
        "--blocksci-bitcoin-blocks-uri",
        "s3://blocks/mainnet",
        "--blocksci-external-bitcoin-datadir",
        "/storage/external/bitcoin",
        "--blocksci-network",
        "bitcoin",
        "--blocksci-max-block",
        "850100",
    ],
    "network without a source": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "reusable",
        "--blocksci-task",
        "parse",
        "--blocksci-network",
        "bitcoin",
    ],
    "script task without a script": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "reusable",
        "--blocksci-task",
        "script",
    ],
    "external task without a baseline": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "reusable",
        "--blocksci-task",
        "external",
    ],
    "source cache for detect": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "cached",
        *SOURCE,
    ],
    "source cache for an analysis without the cached workflow": [
        *READ_ONLY_SCRIPT,
        "--blocksci-workflow",
        "reusable",
        *SOURCE,
    ],
    "analysis reading its own run as the source": [
        *READ_ONLY_SCRIPT,
        "--blocksci-workflow",
        "cached",
        "--blocksci-cache-source-run-id",
        "run-2",
    ],
    "pin height outside a script task": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "cached",
        "--blocksci-task",
        "notebook",
        "--blocksci-pin-height",
        "850000",
    ],
    "empty scan range": [
        *READ_ONLY_SCRIPT,
        "--blocksci-workflow",
        "cached",
        *PINNED_RANGE[:-1],
        "741213",
    ],
    "scan range past the pin": [
        *READ_ONLY_SCRIPT,
        "--blocksci-workflow",
        "cached",
        *PINNED_RANGE[:-1],
        "850002",
    ],
    "multijob index without the multijob resource": PERSISTENT_UPDATE,
    "multijob resource of another location": [*PERSISTENT_UPDATE, "--pbs-blocksci-multijob", "plzen"],
    "multijob for the coinjoin-analysis stage": [
        *PBS_FROM_S3,
        "--analysisPbs",
        "--pbs-analysis-multijob",
        "brno",
    ],
    "index outside shared storage": [
        *PERSISTENT_UPDATE[:-9],
        "/tmp/blocksci-mainnet",
        *PERSISTENT_UPDATE[-8:],
    ],
    "both a persistent index and a source run": [
        *PERSISTENT_UPDATE,
        "--pbs-blocksci-multijob",
        "brno",
        *SOURCE,
    ],
    "index backup outside S3": [
        *PERSISTENT_UPDATE,
        "--pbs-blocksci-multijob",
        "brno",
        "--blocksci-index-backup-uri",
        "/storage/brno2/home/user/copy",
    ],
    "backup task without a backup prefix": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "cached",
        "--blocksci-task",
        "backup",
        "--blocksci-index-dir",
        "/storage/brno2/home/user/blocksci-mainnet",
    ],
    "persistent index for detect": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--blocksci-workflow",
        "cached",
        "--blocksci-index-dir",
        "/storage/brno2/home/user/blocksci-mainnet",
    ],
    "false positives outside the external task": [
        *READ_ONLY_SCRIPT,
        "--blocksci-workflow",
        "cached",
        *SOURCE,
        "--external-false-cjtxs-uri",
        "s3://bucket/dumplings/false_cjtxs.json",
    ],
    "Dumplings baseline without its PBS stage": [
        *PBS_FROM_S3,
        "--blocksciPbs",
        "--dumplings-uri",
        "s3://bucket/dumplings",
    ],
    "report resources without a report job": [
        *PBS_FROM_S3,
        "--analysisPbs",
        "--pbs-unified-report-ncpus",
        "2",
    ],
}


def yaml_equivalent(argv, tmp_path):
    config = load_configuration(argv)
    data = {"action": config.action}
    for path in config._provided - {"action"}:
        set_mapping_path(data, path, config_value(config, path))
    target = tmp_path / "experiment.yaml"
    target.write_text(yaml.safe_dump(data))
    return load_configuration(["run", str(target)])


@pytest.mark.parametrize("argv", VALID.values(), ids=VALID.keys())
def test_every_input_path_accepts(argv, tmp_path):
    direct = resolve_configuration(load_configuration(argv))
    loaded = resolve_configuration(yaml_equivalent(argv, tmp_path))
    assert direct == loaded


@pytest.mark.parametrize("argv", INVALID.values(), ids=INVALID.keys())
def test_every_input_path_rejects(argv, tmp_path):
    for config in (load_configuration(argv), yaml_equivalent(argv, tmp_path)):
        with pytest.raises(ValueError):
            resolve_configuration(config)


@pytest.mark.parametrize(
    "case,message",
    [
        ("source cache for detect", "requires --blocksci-task update, script, external, or notebook"),
        ("source cache for an analysis without the cached workflow", "requires --blocksci-workflow cached"),
        ("analysis reading its own run as the source", "must differ from target --run-id"),
        ("pin height outside a script task", "--blocksci-pin-height requires --blocksci-task script"),
        ("empty scan range", "must be below --blocksci-scan-end-height"),
        ("scan range past the pin", "must not exceed --blocksci-pin-height + 1"),
        ("multijob index without the multijob resource", "requires --pbs-blocksci-multijob brno"),
        ("multijob resource of another location", "requires --pbs-blocksci-multijob brno"),
        ("multijob for the coinjoin-analysis stage", "--pbs-analysis-multijob is supported only for the BlockSci"),
        ("index outside shared storage", "must be under /scratch/multijob-<location>/ or /storage/"),
        ("both a persistent index and a source run", "choose one index source"),
        ("index backup outside S3", "artifact URI must use s3://"),
        ("backup task without a backup prefix", "--blocksci-task backup requires --blocksci-index-backup-uri"),
        ("persistent index for detect", "--blocksci-index-dir requires --blocksci-task parse"),
        ("false positives outside the external task", "--external-false-cjtxs-uri requires --blocksci-task external"),
        ("Dumplings baseline without its PBS stage", "--dumplings-uri requires pbs-from-s3 --analysisPbs"),
    ],
)
def test_read_only_analysis_rules_name_the_violation(case, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        resolve_configuration(load_configuration(INVALID[case]))
