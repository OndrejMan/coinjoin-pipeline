"""Render one persistent-index, Dumplings-baseline or statistics PBS job as pbs-from-s3 would, locally.

The production renderers insist on scripts and block directories on MetaCentrum's shared
storage; here that check is switched off, since the test keeps them in a temporary directory.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
from unittest import mock

from coinjoin_pipeline.configuration import BlockSciConfiguration, PipelineConfiguration
from coinjoin_pipeline.execution.pbs import analysis_modes, templates_s3
from coinjoin_pipeline.execution.pbs.defaults import DEFAULT_RESOURCES
from coinjoin_pipeline.execution.pbs.commands import (
    blocksci_external_report_pbs_command,
    blocksci_parse_pbs_command,
    blocksci_script_pbs_command,
    blocksci_update_pbs_command,
)
from coinjoin_pipeline.storage.s3 import S3Target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("parse", "update", "backup", "script", "compare", "dumplings", "statistics"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--index-dir")
    parser.add_argument("--dumplings-uri")
    parser.add_argument("--coinjoin-type", default="joinmarket")
    parser.add_argument("--input-uri")
    parser.add_argument("--protocol", action="append", default=[])
    parser.add_argument("--backup-uri")
    parser.add_argument("--state-uri", help="fixed S3 prefix of the index jobs in place of runs/<run-id>/")
    parser.add_argument("--bitcoin-datadir", type=Path)
    parser.add_argument("--max-block", type=int)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--pin-height", type=int)
    parser.add_argument("--baseline-uri")
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    target = S3Target(
        artifact_uri="s3://bucket/runs",
        run_id=args.run_id,
        endpoint_url="https://s3.example.invalid",
        credentials_file=args.credentials,
        profile="test",
        state_prefix=args.state_uri,
    )
    with (
        mock.patch.object(templates_s3, "require_storage_path"),
        # The analysis modes check the script's path themselves since they moved out.
        mock.patch.object(analysis_modes, "require_storage_path"),
    ):
        if args.kind == "dumplings":
            script = templates_s3.render_coinjoin_analysis_dumplings_s3_pbs(
                target,
                image="docker://coinjoin-analysis-local",
                dumplings_uri=args.dumplings_uri,
                coinjoin_type=args.coinjoin_type,
            )
            args.output.write_text(script, encoding="utf-8")
            return 0
        if args.kind == "statistics":
            options = PipelineConfiguration.from_mapping(
                {
                    "coinjoin_analysis": {
                        "program": "coinjoin_statistics",
                        "data_mode": "mainnet",
                        "actions": args.protocol,
                        "input_uri": args.input_uri,
                    }
                }
            ).coinjoin_analysis
            script = templates_s3.render_coinjoin_tools_s3_pbs(target, "docker://coinjoin-analysis-local", options)
            args.output.write_text(script, encoding="utf-8")
            return 0
        index = dict(index_dir=args.index_dir, index_backup_uri=args.backup_uri)
        chain = dict(external_bitcoin_datadir=str(args.bitcoin_datadir), network="bitcoin_regtest", max_block=args.max_block)
        short = replace(DEFAULT_RESOURCES["blocksci"], walltime="02:00:00")
        common = dict(image="docker://blocksci-local", resources=short)
        if args.kind == "parse":
            script = templates_s3.render_blocksci_parse_s3_pbs(
                target,
                command=blocksci_parse_pbs_command(
                    args.run_id,
                    coin_type="bitcoin_regtest",
                    disk_path="/mnt/data",
                    max_block_expression=str(args.max_block + 1),
                ),
                blocksci=BlockSciConfiguration(**index, **chain),
                **common,
            )
        elif args.kind == "update":
            script = templates_s3.render_blocksci_update_s3_pbs(
                target,
                command=blocksci_update_pbs_command(args.run_id),
                blocksci=BlockSciConfiguration(**index, **chain),
                **common,
            )
        elif args.kind == "backup":
            script = templates_s3.render_blocksci_index_backup_s3_pbs(
                target, blocksci=BlockSciConfiguration(**index), resources=short
            )
        elif args.kind == "compare":
            config = PipelineConfiguration.from_flat(
                {"coinjoin_type": "wasabi2", "min_input_count": 15, "blocksci_pin_height": args.pin_height}
            )
            script = templates_s3.render_blocksci_analyze_s3_pbs(
                target,
                command=blocksci_external_report_pbs_command(args.run_id, config),
                mode="blocksci-external",
                blocksci=BlockSciConfiguration(**index, external_baseline_uri=args.baseline_uri),
                **common,
            )
        else:
            config = PipelineConfiguration.from_flat(
                {"coinjoin_type": "wasabi2", "min_input_count": 15, "blocksci_pin_height": args.pin_height}
            )
            script = templates_s3.render_blocksci_analyze_s3_pbs(
                target,
                command=blocksci_script_pbs_command(args.run_id, config),
                mode="blocksci-script",
                blocksci=BlockSciConfiguration(**index, script=str(args.script)),
                **common,
            )
    args.output.write_text(script, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
