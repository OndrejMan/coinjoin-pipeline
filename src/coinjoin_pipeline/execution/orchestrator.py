"""Dispatch a resolved configuration to the experimental local paths in ``local.py``."""

from __future__ import annotations

from coinjoin_pipeline.configuration import PipelineConfiguration

from . import local


def run_configuration(args: PipelineConfiguration) -> None:
    local.run_configuration(args, args.runs_path)
