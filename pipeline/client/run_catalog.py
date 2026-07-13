"""Run discovery, provenance, and stage state for researcher-facing commands."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "research_manifest.json"
REPORT_DIR = "blocksciEmulatorAnalysis_data"
BASELINE_FILE = "coinjoin-analysis_data/coinjoin_tx_info.json"
FALSE_CJTXS_FILE = "coinjoin-analysis_data/false_cjtxs.json"


@dataclass(frozen=True)
class RunState:
    run_dir: Path
    mode: str
    stages: dict[str, bool]
    manifest: dict[str, object]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def load_manifest(run_dir: Path) -> dict[str, object]:
    path = run_dir / MANIFEST_NAME
    return load_json(path) if path.is_file() else {}


def is_run_dir(path: Path) -> bool:
    return path.is_dir() and (
        (path / MANIFEST_NAME).is_file()
        or (path / "coinjoin_emulator_data").is_dir()
        or (path / "blocksci_data").is_dir()
    )


def stage_state(run_dir: Path) -> dict[str, bool]:
    report_dir = run_dir / REPORT_DIR
    return {
        "emulation": (run_dir / "coinjoin_emulator_data").is_dir(),
        "baseline": (run_dir / BASELINE_FILE).is_file(),
        "blocksci": (run_dir / "blocksci_data/config.json").is_file(),
        "report": (report_dir / "unified_report.json").is_file(),
        "markdown": (report_dir / "unified_report.md").is_file(),
        "mappings": (run_dir / "coinjoin-mappings_data" / "coinjoin_mappings.json").is_file(),
    }


def report_status(run_dir: Path) -> str:
    report_path = run_dir / REPORT_DIR / "unified_report.json"
    if not report_path.is_file():
        return "missing"
    try:
        report = load_json(report_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return "invalid"
    if report.get("evaluation_scope") == "baseline_agreement_only":
        return "baseline_agreement_only"
    if report.get("evaluation_scope") == "emulator_labels_unavailable":
        return "emulator_labels_unavailable"
    diagnostics = report.get("integration_diagnostics")
    if not isinstance(diagnostics, dict):
        return "diagnostics_missing"
    if isinstance(diagnostics, dict) and diagnostics.get("status") == "not_ok":
        return "diagnostics_not_ok"
    return "complete"


def mode_for_run(run_dir: Path, manifest: dict[str, object] | None = None) -> str:
    manifest = manifest if manifest is not None else load_manifest(run_dir)
    mode = manifest.get("mode")
    if mode in {"emulator", "external"}:
        return str(mode)
    return "emulator" if (run_dir / "coinjoin_emulator_data").is_dir() else "unknown"


def discover_runs(runs_root: Path) -> list[RunState]:
    if not runs_root.is_dir():
        return []
    states = []
    for path in sorted((item for item in runs_root.iterdir() if is_run_dir(item)), key=lambda item: item.name):
        manifest = load_manifest(path)
        states.append(RunState(path, mode_for_run(path, manifest), stage_state(path), manifest))
    return states


def create_external_manifest(
    run_dir: Path,
    bitcoin_datadir: Path,
    baseline: Path,
    network: str,
    coinjoin_type: str,
    false_cjtxs: list[Path] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "external",
        "run_id": run_dir.name,
        "network": network,
        "coinjoin_type": coinjoin_type,
        "inputs": {
            "bitcoin_datadir": str(bitcoin_datadir.resolve()),
            "baseline": str(baseline.resolve()),
            "baseline_sha256": sha256_file(baseline),
            "false_cjtxs": [
                {
                    "path": str(path.resolve()),
                    "sha256": sha256_file(path),
                }
                for path in (false_cjtxs or [])
            ],
        },
    }


def write_manifest(run_dir: Path, manifest: dict[str, object], overwrite: bool = False) -> None:
    target = run_dir / MANIFEST_NAME
    if target.exists() and not overwrite:
        raise FileExistsError(f"Run manifest already exists: {target}; use --resume to reuse this run.")
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
