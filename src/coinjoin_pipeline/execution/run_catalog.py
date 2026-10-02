"""EXPERIMENTAL: Run discovery, provenance, and stage state for researcher-facing commands.

Not part of the Kubernetes → S3 → PBS path that the thesis results come from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from exporters.artifact_paths import (
    ANALYSIS_ARTIFACT_FILE,
    BASELINE_FILE,
    BLOCKSCI_CONFIG_FILE,
    CATALOG_MARKERS,
    EMULATOR_DIR,
    MAPPINGS_FILE,
    REPORT_FILE,
    REPORT_MARKDOWN_FILE,
    RUN_MANIFEST,
    report_input_files,
)
from exporters.common import file_sha256, load_json

from coinjoin_pipeline.execution.run_context import is_run_dir as has_marker
from coinjoin_pipeline.manifest import atomic_write

StageStatus = Literal["missing", "stale", "present"]
ReportStatus = Literal[
    "missing",
    "invalid",
    "stale",
    "baseline_agreement_only",
    "emulator_labels_unavailable",
    "diagnostics_missing",
    "diagnostics_not_ok",
    "complete",
]
# Inputs compared by modification time for reports that predate recorded input hashes.
LEGACY_REPORT_INPUTS = (
    EMULATOR_DIR,
    BASELINE_FILE,
    BLOCKSCI_CONFIG_FILE,
    ANALYSIS_ARTIFACT_FILE,
    MAPPINGS_FILE,
)


class RunManifest(TypedDict, total=False):
    """``research_manifest.json``: one shape for emulator and external runs."""

    schema_version: int
    mode: str
    run_id: str
    # Emulator runs: the redacted launcher record written by the host CLI.
    host_launcher: dict[str, object]
    # External runs: the imported chain and baseline provenance.
    network: str
    coinjoin_type: str
    inputs: dict[str, object]


@dataclass(frozen=True)
class RunState:
    run_dir: Path
    mode: str
    stages: dict[str, StageStatus]
    report_status: ReportStatus
    manifest: RunManifest


def load_manifest(run_dir: Path) -> RunManifest:
    path = run_dir / RUN_MANIFEST
    return cast(RunManifest, load_json(path)) if path.is_file() else {}


def is_run_dir(path: Path) -> bool:
    return has_marker(path, CATALOG_MARKERS)


def _recorded_inputs(report: dict[str, object]) -> dict[str, str] | None:
    run_manifest = report.get("run_manifest")
    inputs = run_manifest.get("inputs") if isinstance(run_manifest, dict) else None
    return inputs if isinstance(inputs, dict) else None


def _report_is_stale(run_dir: Path, report: dict[str, object]) -> bool:
    """Whether the report's inputs changed after it was written."""
    recorded = _recorded_inputs(report)
    if recorded is None:
        report_mtime = (run_dir / REPORT_FILE).stat().st_mtime
        return any(
            (run_dir / name).exists() and (run_dir / name).stat().st_mtime > report_mtime
            for name in LEGACY_REPORT_INPUTS
        )
    # An input missing locally (an S3 run with only the report downloaded) is no
    # evidence of change; a new or rewritten one is.
    for name in report_input_files(run_dir):
        if recorded.get(name) != file_sha256(run_dir / name):
            return True
    return False


def report_status(run_dir: Path) -> ReportStatus:
    report_path = run_dir / REPORT_FILE
    if not report_path.is_file():
        return "missing"
    try:
        report = load_json(report_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return "invalid"
    if _report_is_stale(run_dir, report):
        return "stale"
    if report.get("evaluation_scope") == "baseline_agreement_only":
        return "baseline_agreement_only"
    if report.get("evaluation_scope") == "emulator_labels_unavailable":
        return "emulator_labels_unavailable"
    diagnostics = report.get("integration_diagnostics")
    if not isinstance(diagnostics, dict):
        return "diagnostics_missing"
    # Fail closed: only an explicit "ok" is complete, so a renamed or new status
    # value from a future exporter reads as a problem, not as passing.
    if diagnostics.get("status") != "ok":
        return "diagnostics_not_ok"
    return "complete"


def stage_state(run_dir: Path, report: ReportStatus | None = None) -> dict[str, StageStatus]:
    """Each stage as missing, present, or — for the report outputs — stale."""
    report = report_status(run_dir) if report is None else report

    def present(relative: str) -> StageStatus:
        return "present" if (run_dir / relative).exists() else "missing"

    report_stage: StageStatus = "missing" if report == "missing" else "stale" if report == "stale" else "present"
    markdown = present(REPORT_MARKDOWN_FILE)
    return {
        "emulation": present(EMULATOR_DIR),
        "baseline": present(BASELINE_FILE),
        "blocksci": present(BLOCKSCI_CONFIG_FILE),
        "report": report_stage,
        "markdown": "stale" if markdown == "present" and report == "stale" else markdown,
        "mappings": present(MAPPINGS_FILE),
    }


def mode_for_run(run_dir: Path, manifest: RunManifest | None = None) -> str:
    manifest = manifest if manifest is not None else load_manifest(run_dir)
    mode = manifest.get("mode")
    if mode in {"emulator", "external"}:
        return str(mode)
    return "emulator" if (run_dir / EMULATOR_DIR).is_dir() else "unknown"


def discover_runs(runs_root: Path) -> list[RunState]:
    if not runs_root.is_dir():
        return []
    states = []
    for path in sorted((item for item in runs_root.iterdir() if is_run_dir(item)), key=lambda item: item.name):
        manifest = load_manifest(path)
        status = report_status(path)
        states.append(RunState(path, mode_for_run(path, manifest), stage_state(path, status), status, manifest))
    return states


def create_external_manifest(
    run_dir: Path,
    bitcoin_datadir: Path,
    baseline: Path,
    network: str,
    coinjoin_type: str,
    false_cjtxs: list[Path] | None = None,
) -> RunManifest:
    return {
        "schema_version": 1,
        "mode": "external",
        "run_id": run_dir.name,
        "network": network,
        "coinjoin_type": coinjoin_type,
        "inputs": {
            "bitcoin_datadir": str(bitcoin_datadir.resolve()),
            "baseline": str(baseline.resolve()),
            "baseline_sha256": file_sha256(baseline),
            "false_cjtxs": [
                {
                    "path": str(path.resolve()),
                    "sha256": file_sha256(path),
                }
                for path in (false_cjtxs or [])
            ],
        },
    }


def write_manifest(run_dir: Path, manifest: RunManifest) -> None:
    target = run_dir / RUN_MANIFEST
    if target.exists():
        raise FileExistsError(f"Run manifest already exists: {target}")
    atomic_write(target, manifest)
