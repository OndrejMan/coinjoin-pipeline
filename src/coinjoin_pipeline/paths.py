"""Canonical locations of checkout resources and packaged orchestration."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
PIPELINE_ROOT = PROJECT_ROOT / "pipeline"
EXECUTION_ROOT = PACKAGE_ROOT / "execution"
EXPORTERS_ROOT = PIPELINE_ROOT / "exporters"
SCENARIOS_ROOT = PROJECT_ROOT / "scenarios"
CONTAINER_ROOT = PROJECT_ROOT / "container"


def default_runs_root() -> Path:
    """The runs root when none is configured: $EMULATION_LOGS_DIR, else ./coinjoin-runs."""
    return Path(os.environ.get("EMULATION_LOGS_DIR") or Path.cwd() / "coinjoin-runs").expanduser().resolve()
