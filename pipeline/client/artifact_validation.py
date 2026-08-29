"""Cross-option validation for artifact-backed pipeline commands."""

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
    backend = getattr(args, "artifact_backend", "shared-storage")
    blocksci_workflow = getattr(args, "blocksci_workflow", "combined")
    blocksci_task = getattr(args, "blocksci_task", "detect")
    if blocksci_workflow != "combined" and not getattr(args, "blocksciPbs", False):
        parser.error("reusable BlockSci workflows require --blocksciPbs")
    if blocksci_task == "parse":
        if args.action != "pbs-from-s3" or blocksci_workflow != "reusable":
            parser.error("--blocksci-task parse requires pbs-from-s3 --blocksci-workflow reusable")
        if getattr(args, "analysisPbs", False) or not getattr(args, "blocksciPbs", False):
            parser.error("--blocksci-task parse requires --blocksciPbs without --analysisPbs")
    elif blocksci_task == "update":
        if args.action != "pbs-from-s3" or blocksci_workflow != "cached":
            parser.error("--blocksci-task update requires pbs-from-s3 --blocksci-workflow cached")
        if getattr(args, "analysisPbs", False) or not getattr(args, "blocksciPbs", False):
            parser.error("--blocksci-task update requires --blocksciPbs without --analysisPbs")
    elif blocksci_task not in {"detect", "external"}:
        if args.action != "pbs-from-s3":
            parser.error("BlockSci reusable tasks are submitted with pbs-from-s3")
        if blocksci_workflow == "combined":
            parser.error("BlockSci reusable tasks require --blocksci-workflow reusable or cached")
        if getattr(args, "analysisPbs", False) or not getattr(args, "blocksciPbs", False):
            parser.error("BlockSci reusable tasks require --blocksciPbs without --analysisPbs")
    if args.action == "pbs-from-s3" and blocksci_task == "script" and not getattr(args, "blocksci_script", None):
        parser.error("--blocksci-task script requires --blocksci-script")
    if args.action == "pbs-from-s3" and blocksci_task != "script" and getattr(args, "blocksci_script", None):
        parser.error("--blocksci-script requires --blocksci-task script")
    if blocksci_task != "notebook" and getattr(args, "blocksci_notebooks_dir", None):
        parser.error("--blocksci-notebooks-dir requires --blocksci-task notebook")
    raw_notebook_port = getattr(args, "blocksci_notebook_port", None)
    if blocksci_task != "notebook" and raw_notebook_port is not None:
        parser.error("--blocksci-notebook-port requires --blocksci-task notebook")
    notebook_port = raw_notebook_port or 8888
    if not 1024 <= notebook_port <= 65535:
        parser.error("--blocksci-notebook-port must be between 1024 and 65535")
    external_bitcoin = getattr(args, "blocksci_external_bitcoin_datadir", None)
    bitcoin_blocks_uri = getattr(args, "blocksci_bitcoin_blocks_uri", None)
    external_index = getattr(args, "blocksci_external_blocksci_dir", None)
    external_network = getattr(args, "blocksci_network", None)
    external_max_block = getattr(args, "blocksci_max_block", None)
    source_cache_run_id = getattr(args, "blocksci_cache_source_run_id", None)
    if blocksci_task == "update":
        if not source_cache_run_id:
            parser.error("--blocksci-task update requires --blocksci-cache-source-run-id")
        if not external_bitcoin:
            parser.error("--blocksci-task update requires --blocksci-external-bitcoin-datadir")
        if external_index:
            parser.error("--blocksci-task update does not support --blocksci-external-blocksci-dir")
    elif source_cache_run_id:
        parser.error("--blocksci-cache-source-run-id requires --blocksci-task update")
    if sum(bool(value) for value in (external_bitcoin, bitcoin_blocks_uri, external_index)) > 1:
        parser.error(
            "choose only one BlockSci source: --blocksci-external-bitcoin-datadir, "
            "--blocksci-bitcoin-blocks-uri, or --blocksci-external-blocksci-dir"
        )
    if external_bitcoin or bitcoin_blocks_uri or external_index:
        parse_source = (
            args.action == "pbs-from-s3"
            and blocksci_workflow == "reusable"
            and blocksci_task == "parse"
        )
        update_source = (
            args.action == "pbs-from-s3"
            and blocksci_workflow == "cached"
            and blocksci_task == "update"
            and bool(external_bitcoin)
            and not external_index
        )
        if not (parse_source or update_source):
            parser.error(
                "external BlockSci sources require either reusable parse or cached update"
            )
    if external_bitcoin or bitcoin_blocks_uri:
        if external_network is None or external_max_block is None:
            parser.error(
                "an external Bitcoin source requires --blocksci-network and "
                "--blocksci-max-block"
            )
    elif external_network is not None or external_max_block is not None:
        parser.error(
            "--blocksci-network and --blocksci-max-block require "
            "an external Bitcoin source"
        )
    baseline_uri = getattr(args, "external_baseline_uri", None)
    if blocksci_task == "external":
        if args.action != "pbs-from-s3" or blocksci_workflow == "combined":
            parser.error("--blocksci-task external requires pbs-from-s3 with reusable or cached workflow")
        if getattr(args, "analysisPbs", False) or not getattr(args, "blocksciPbs", False):
            parser.error("--blocksci-task external requires --blocksciPbs without --analysisPbs")
        if not baseline_uri:
            parser.error("--blocksci-task external requires --external-baseline-uri")
    elif baseline_uri:
        parser.error("--external-baseline-uri requires --blocksci-task external")
    if args.action == "pbs-from-s3":
        args.artifact_backend = "s3"
        required = (
            ("artifact_uri", "--artifact-uri"),
            ("s3_endpoint_url", "--s3-endpoint-url"),
            ("run_id", "--run-id"),
            ("s3_credentials_file", "--s3-credentials-file"),
            ("s3_profile", "--s3-profile"),
        )
        for attribute, flag in required:
            if not getattr(args, attribute, None):
                parser.error(f"pbs-from-s3 requires {flag}")
        if not args.analysisPbs and not args.blocksciPbs and not args.mappingsPbs:
            parser.error(
                "pbs-from-s3 requires --analysisPbs, --blocksciPbs, or --mappingsPbs"
            )
        report_resource_options = (
            "pbs_unified_report_ncpus",
            "pbs_unified_report_mem",
            "pbs_unified_report_scratch",
            "pbs_unified_report_walltime",
        )
        separate_report = args.blocksciPbs and blocksci_task == "detect" and (
            args.analysisPbs or args.mappingsPbs or blocksci_workflow != "combined"
        )
        if any(getattr(args, option, None) is not None for option in report_resource_options) and not separate_report:
            parser.error(
                "unified-report PBS resource overrides require a separate unified-report job"
            )
    elif backend == "s3":
        if args.action == "full-run":
            if blocksci_workflow == "cached":
                parser.error("full-run cannot reuse a cache before emulation; use --blocksci-workflow reusable")
            if getattr(args, "driver", None) != "kubernetes":
                parser.error("full-run --artifact-backend s3 requires --driver kubernetes")
            for attribute, flag in (
                ("artifact_uri", "--artifact-uri"),
                ("s3_endpoint_url", "--s3-endpoint-url"),
                ("run_id", "--run-id"),
                ("s3_secret_name", "--s3-secret-name"),
                ("s3_credentials_file", "--s3-credentials-file"),
                ("s3_profile", "--s3-profile"),
            ):
                if not getattr(args, attribute, None):
                    parser.error(f"full-run --artifact-backend s3 requires {flag}")
            if not args.analysisPbs or not args.blocksciPbs:
                parser.error("full-run --artifact-backend s3 requires both --analysisPbs and --blocksciPbs")
            if not getattr(args, "reuse_namespace", False):
                parser.error(
                    "Kubernetes S3-compatible mode requires --reuse-namespace because "
                    "the credentials Secret must exist before the Job is created"
                )
            if getattr(args, "parallel", False):
                parser.error(
                    "full-run --artifact-backend s3 does not support --parallel "
                    "because its analyzer jobs already run in parallel"
                )
            if getattr(args, "blocksci_script", None):
                parser.error("full-run --artifact-backend s3 does not support --blocksci-script")
        elif args.action != "emulate" or getattr(args, "driver", None) != "kubernetes":
            parser.error("--artifact-backend s3 is supported only by full-run and emulate with --driver kubernetes")
        else:
            for attribute, flag in (
                ("artifact_uri", "--artifact-uri"),
                ("s3_endpoint_url", "--s3-endpoint-url"),
                ("run_id", "--run-id"),
                ("s3_secret_name", "--s3-secret-name"),
                # The frontend stages the exporters itself, so it needs its own
                # credentials, not only the in-cluster Secret.
                ("s3_credentials_file", "--s3-credentials-file"),
                ("s3_profile", "--s3-profile"),
            ):
                if not getattr(args, attribute, None):
                    parser.error(f"Kubernetes S3-compatible mode requires {flag}")
            if not getattr(args, "reuse_namespace", False):
                parser.error(
                    "Kubernetes S3-compatible mode requires --reuse-namespace because "
                    "the credentials Secret must exist before the Job is created"
                )
        if (
            getattr(args, "kubernetes_btc_datadir", None)
            or getattr(args, "pbs_bitcoin_datadir", None)
            or getattr(args, "copy_to_host", False)
        ):
            parser.error(
                "Kubernetes S3-compatible mode does not support --kubernetes-btc-datadir, "
                "--pbs-bitcoin-datadir, or --copy-to-host"
            )
    elif blocksci_workflow != "combined" or blocksci_task != "detect":
        parser.error("reusable BlockSci workflows are currently supported only with the S3 artifact backend")
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

