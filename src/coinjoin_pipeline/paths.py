"""Canonical locations of checkout resources and packaged orchestration."""

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
PIPELINE_ROOT = PROJECT_ROOT / "pipeline"
EXECUTION_ROOT = PACKAGE_ROOT / "execution"
EXPORTERS_ROOT = PIPELINE_ROOT / "exporters"
SCENARIOS_ROOT = PROJECT_ROOT / "scenarios"
CONTAINER_ROOT = PROJECT_ROOT / "container"
