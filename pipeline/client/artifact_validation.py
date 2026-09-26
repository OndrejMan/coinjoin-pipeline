"""Effective-value validation and normalization for artifact-backed commands."""

from __future__ import annotations

import argparse

from client.artifacts import (
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
    validate_s3_profile,
)


def validate_artifact_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Check the effective artifact values argparse resolved, then normalize them.

    The cross-option rules live in ``coinjoin_pipeline.option_rules`` and have
    already run on the explicit argv (``cli_entrypoint._validate_request``).
    Only what argv cannot show is left here: the notebook port range, a Bitcoin
    datadir defaulted from ``PBS_BITCOIN_DATADIR``, and the value validators.
    """
    backend = getattr(args, "artifact_backend", "shared-storage")
    notebook_port = getattr(args, "blocksci_notebook_port", None) or 8888
    if not 1024 <= notebook_port <= 65535:
        parser.error("--blocksci-notebook-port must be between 1024 and 65535")
    source_cache_run_id = getattr(args, "blocksci_cache_source_run_id", None)
    if args.action == "pbs-from-s3":
        args.artifact_backend = "s3"
    elif backend == "s3" and (
        getattr(args, "kubernetes_btc_datadir", None)
        or getattr(args, "pbs_bitcoin_datadir", None)
        or getattr(args, "copy_to_host", False)
    ):
        parser.error(
            "Kubernetes S3-compatible mode does not support --kubernetes-btc-datadir, "
            "--pbs-bitcoin-datadir, or --copy-to-host"
        )
    try:
        if getattr(args, "artifact_uri", None):
            args.artifact_uri = validate_artifact_uri(args.artifact_uri)
        if getattr(args, "s3_endpoint_url", None):
            args.s3_endpoint_url = validate_s3_endpoint_url(args.s3_endpoint_url)
        if getattr(args, "run_id", None):
            args.run_id = validate_run_id(args.run_id)
        if source_cache_run_id:
            args.blocksci_cache_source_run_id = validate_run_id(source_cache_run_id)
            if args.blocksci_cache_source_run_id == args.run_id:
                parser.error(
                    "--blocksci-cache-source-run-id must differ from target --run-id"
                )
        if getattr(args, "s3_credentials_file", None):
            args.s3_credentials_file = validate_credentials_file(args.s3_credentials_file)
        if getattr(args, "s3_profile", None):
            args.s3_profile = validate_s3_profile(args.s3_profile)
    except ValueError as error:
        parser.error(str(error))

