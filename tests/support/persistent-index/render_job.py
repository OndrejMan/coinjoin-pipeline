"""Render one persistent-index or Dumplings-baseline PBS job as pbs-from-s3 would, for a local run.

The production renderers insist on MetaCentrum paths (/storage, /scratch/multijob-*);
here those roots point at the test's temporary directory instead.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest import mock

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.pbs import templates_s3
from coinjoin_pipeline.execution.pbs.commands import (
    blocksci_external_report_pbs_command,
    blocksci_parse_pbs_command,
    blocksci_script_pbs_command,
    blocksci_update_pbs_command,
)
from coinjoin_pipeline.storage.s3 import S3Target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("parse", "update", "backup", "script", "compare", "dumplings"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, required=True, help="stands in for /storage and multijob scratch")
    parser.add_argument("--index-dir")
    parser.add_argument("--dumplings-uri")
    parser.add_argument("--coinjoin-type", default="joinmarket")
    parser.add_argument("--backup-uri")
    parser.add_argument("--bitcoin-datadir", type=Path)
    parser.add_argument("--max-block", type=int)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--pin-height", type=int)
    parser.add_argument("--baseline-uri")
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = f"{args.root.resolve()}/"
    target = S3Target(
        artifact_uri="s3://bucket/runs",
        run_id=args.run_id,
        endpoint_url="https://s3.example.invalid",
        credentials_file=args.credentials,
        profile="test",
    )
    with (
        mock.patch.object(templates_s3, "INDEX_ROOTS", (root,)),
        mock.patch.object(templates_s3, "require_storage_path"),
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
        index = templates_s3.PersistentIndex(args.index_dir, args.backup_uri)
        common = dict(image="docker://blocksci-local", persistent=index, walltime="02:00:00")
        if args.kind == "parse":
            script = templates_s3.render_blocksci_parse_s3_pbs(
                target,
                command=blocksci_parse_pbs_command(
                    args.run_id,
                    coin_type="bitcoin_regtest",
                    disk_path="/mnt/data",
                    max_block_expression=str(args.max_block + 1),
                ),
                external_bitcoin_datadir=args.bitcoin_datadir,
                external_network="bitcoin_regtest",
                external_max_block=args.max_block,
                **common,
            )
        elif args.kind == "update":
            script = templates_s3.render_blocksci_update_s3_pbs(
                target,
                None,
                command=blocksci_update_pbs_command(args.run_id),
                external_bitcoin_datadir=args.bitcoin_datadir,
                external_network="bitcoin_regtest",
                external_max_block=args.max_block,
                **common,
            )
        elif args.kind == "backup":
            script = templates_s3.render_blocksci_index_backup_s3_pbs(target, persistent=index, walltime="02:00:00")
        elif args.kind == "compare":
            config = PipelineConfiguration.from_flat(
                {"coinjoin_type": "wasabi2", "min_input_count": 15, "blocksci_pin_height": args.pin_height}
            )
            script = templates_s3.render_blocksci_analyze_s3_pbs(
                target,
                command=blocksci_external_report_pbs_command(args.run_id, config),
                mode="blocksci-external",
                external_baseline_uri=args.baseline_uri,
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
                user_script=args.script,
                **common,
            )
    args.output.write_text(script, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
