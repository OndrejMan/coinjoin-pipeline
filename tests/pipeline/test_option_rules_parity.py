"""Equivalent configuration policy for YAML and direct command-line values."""

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
    "S3 emulation": ["emulate", *S3_EMULATION, *S3],
    "S3 full-run": ["full-run", *S3_EMULATION, *S3, "--analysisPbs", "--blocksciPbs"],
}

INVALID = {
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
