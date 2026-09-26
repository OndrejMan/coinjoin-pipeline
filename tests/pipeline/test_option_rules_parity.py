"""Every input path must accept and reject the same commands.

The host CLI, the wrapper entrypoint, and the interactive command builder all
apply ``coinjoin_pipeline.option_rules``.  These cases pin that contract: each
invalid command was once caught by only one path's private copy of a rule.
"""

import os
import shlex
import sys
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))

from coinjoin_pipeline.builder import parse_command, validate_command  # noqa: E402
from coinjoin_pipeline.commands import action_from, validate_passthrough  # noqa: E402

from client import cli_entrypoint  # noqa: E402
from client.wrapper import build_parser, wrapper_operations  # noqa: E402

S3 = [
    "--artifact-uri", "s3://bucket/runs",
    "--s3-endpoint-url", "https://s3.example.invalid",
    "--s3-credentials-file", "/storage/user/.aws/credentials",
    "--s3-profile", "coinjoin",
]
PBS_FROM_S3 = ["pbs-from-s3", "--run-id", "run-2", "--engine", "wasabi", *S3]
S3_EMULATION = [
    "--engine", "wasabi", "--driver", "kubernetes", "--artifact-backend", "s3",
    "--run-id", "run-1", "--s3-secret-name", "coinjoin-s3", "--reuse-namespace",
]

VALID = {
    "analyze": ["analyze", "--engine", "wasabi", "--run-dir", "run-1"],
    "reusable parse from a bitcoin-blocks URI": [
        *PBS_FROM_S3, "--blocksciPbs", "--blocksci-workflow", "reusable",
        "--blocksci-task", "parse", "--blocksci-bitcoin-blocks-uri", "s3://blocks/mainnet",
        "--blocksci-network", "bitcoin", "--blocksci-max-block", "850100",
    ],
    "cached update": [
        *PBS_FROM_S3, "--blocksciPbs", "--blocksci-workflow", "cached",
        "--blocksci-task", "update", "--blocksci-cache-source-run-id", "run-1",
        "--blocksci-external-bitcoin-datadir", "/storage/external/bitcoin",
        "--blocksci-network", "bitcoin", "--blocksci-max-block", "850100",
    ],
    "S3 emulation": ["emulate", *S3_EMULATION, *S3],
    "S3 full-run": ["full-run", *S3_EMULATION, *S3, "--analysisPbs", "--blocksciPbs"],
}

INVALID = {
    "mappings without its PBS stage": ["mappings", "--engine", "wasabi", "--run-dir", "run-1"],
    "mappings with JoinMarket": ["full-run", "--engine", "joinmarket", "--mappingsPbs"],
    "mappings with a JoinMarket CoinJoin type": [
        "mappings", "--engine", "wasabi", "--coinjoin-type", "joinmarket",
        "--run-dir", "run-1", "--mappingsPbs",
    ],
    "unconfirmed clean": ["clean"],
    "S3 emulation without frontend credentials": ["emulate", *S3_EMULATION, *S3[:4]],
    "S3 emulation with an empty artifact URI": [
        "emulate", *S3_EMULATION, "--artifact-uri", "", *S3[2:],
    ],
    "S3 full-run in parallel": [
        "full-run", *S3_EMULATION, *S3, "--analysisPbs", "--blocksciPbs", "--parallel",
    ],
    "reusable BlockSci on shared storage": [
        "full-run", "--engine", "wasabi", "--blocksciPbs", "--pbs-bitcoin-datadir",
        "/storage/btc", "--blocksci-workflow", "reusable",
    ],
    "update without a source cache": [
        *PBS_FROM_S3, "--blocksciPbs", "--blocksci-workflow", "cached",
        "--blocksci-task", "update", "--blocksci-external-bitcoin-datadir",
        "/storage/external/bitcoin", "--blocksci-network", "bitcoin",
        "--blocksci-max-block", "850100",
    ],
    "two BlockSci sources": [
        *PBS_FROM_S3, "--blocksciPbs", "--blocksci-workflow", "reusable",
        "--blocksci-task", "parse", "--blocksci-bitcoin-blocks-uri", "s3://blocks/mainnet",
        "--blocksci-external-bitcoin-datadir", "/storage/external/bitcoin",
        "--blocksci-network", "bitcoin", "--blocksci-max-block", "850100",
    ],
    "network without a source": [
        *PBS_FROM_S3, "--blocksciPbs", "--blocksci-workflow", "reusable",
        "--blocksci-task", "parse", "--blocksci-network", "bitcoin",
    ],
    "script task without a script": [
        *PBS_FROM_S3, "--blocksciPbs", "--blocksci-workflow", "reusable",
        "--blocksci-task", "script",
    ],
    "external task without a baseline": [
        *PBS_FROM_S3, "--blocksciPbs", "--blocksci-workflow", "reusable",
        "--blocksci-task", "external",
    ],
    "report resources without a report job": [
        *PBS_FROM_S3, "--analysisPbs", "--pbs-unified-report-ncpus", "2",
    ],
}


def host_errors(argv: list[str]) -> list[str]:
    return validate_passthrough(argv, action_from(argv))


def builder_errors(argv: list[str]) -> list[str]:
    return validate_command(parse_command(f"coinjoin-pipeline {shlex.join(argv)}")).errors


def wrapper_rejects(argv: list[str]) -> bool:
    environment = {key: value for key, value in os.environ.items() if key != "PBS_BITCOIN_DATADIR"}
    with mock.patch.dict(os.environ, environment, clear=True):
        parser = build_parser()
        try:
            args = parser.parse_args(argv)
            cli_entrypoint._validate_request(wrapper_operations(), parser, args, argv)
        except SystemExit:
            return True
    return False


@pytest.mark.parametrize("argv", VALID.values(), ids=VALID.keys())
def test_every_input_path_accepts(argv: list[str]) -> None:
    assert host_errors(argv) == []
    assert builder_errors(argv) == []
    assert not wrapper_rejects(argv)


@pytest.mark.parametrize("argv", INVALID.values(), ids=INVALID.keys())
def test_every_input_path_rejects(argv: list[str]) -> None:
    assert host_errors(argv)
    assert builder_errors(argv)
    assert wrapper_rejects(argv)
