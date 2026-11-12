#!/usr/bin/env python3
"""Print a PBS job proving the mainnet S3 backups restore; nothing is submitted."""
from datetime import datetime, timezone
from pathlib import Path
import sys

import yaml

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.context import resolve_configuration
from coinjoin_pipeline.execution.mainnet_sync import render_restore_check


def main() -> None:
    path = Path(sys.argv[1])
    mapping = yaml.safe_load(path.read_text(encoding="utf-8"))
    mapping["run_id"] = "mainnet-restore-check-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    print(render_restore_check(resolve_configuration(PipelineConfiguration.from_mapping(mapping))))


if __name__ == "__main__":
    main()
