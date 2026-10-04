"""Command-line entrypoint for unified report export."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from exporters.analysis_artifact import ARTIFACT_NAME
from exporters.analysis_artifact import detector_parameters as blocksci_detector_parameters
from exporters.analysis_artifact import load_analysis as load_blocksci_analysis
from exporters.artifact_paths import (
    BASELINE_FILE,
    REPORT_JSON,
    SCENARIO_FILE,
    blocksci_analysis_dir,
    coinjoin_analysis_dir,
    mappings_dir,
    report_dir,
)
from exporters.common import (
    digest_from_reference,
    load_json,
    save_json,
)
from exporters.emulator_data import build_emulator_data
from exporters.normalization import (
    fill_missing_block_heights,
    filter_coinjoin_analysis_false_positives,
    load_exported_block_tx_index,
    load_false_positive_txids,
    normalize_coinjoin_analysis,
)
from exporters.parameters import add_detector_arguments, add_image_arguments
from exporters.report_builder import build_report
from exporters.scenario import load_scenario


def find_latest_run_dir(runs_root: Path) -> Path:
    candidates = [child for child in runs_root.iterdir() if child.is_dir() and (child / SCENARIO_FILE).exists()]
    if not candidates:
        raise FileNotFoundError(f"No emulation run folders found under {runs_root}")
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    print(f"[WARN] No --run-dir provided; using newest run folder: {latest}", file=sys.stderr)
    return latest


def resolve_run_dir(runs_root: Path, run_dir_arg: str | Path | None) -> Path:
    if run_dir_arg is None or not str(run_dir_arg).strip():
        return find_latest_run_dir(runs_root).resolve()
    run_dir = Path(run_dir_arg).expanduser()
    if not run_dir.is_absolute():
        run_dir = runs_root.expanduser() / run_dir
    return run_dir.resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a unified BlockSci-vs-emulator JSON report.")
    parser.add_argument("--config", type=Path, help="BlockSci config; defaults to the selected run config.")
    parser.add_argument("--runs-root", type=Path, default=Path("/runs/emulation/logs"))
    parser.add_argument("--run-dir", help="Run folder path or name under --runs-root.")
    parser.add_argument("--scenario", type=Path, help="Fallback scenario JSON path if run folder has none.")
    parser.add_argument("--engine", default=os.environ.get("COINJOIN_ENGINE"))
    parser.add_argument(
        "--mode",
        choices=("emulator", "external"),
        default="emulator",
        help="Report mode. External mode compares analyzer agreement without emulator ground truth.",
    )
    parser.add_argument("--network", default=None, help="Network name recorded for external-chain reports.")
    parser.add_argument("--output-name", default=REPORT_JSON)
    parser.add_argument("--emulator-git-commit", default=os.environ.get("COINJOIN_EMULATOR_GIT_COMMIT"))
    parser.add_argument(
        "--cluster-output-dir",
        type=Path,
        help="Directory for temporary BlockSci CoinJoin clustering data.",
    )
    parser.add_argument(
        "--skip-clustering",
        action="store_true",
        help="Skip BlockSci cluster assignment export and only report detection metrics.",
    )
    parser.add_argument(
        "--blocksci-analysis",
        type=Path,
        help="Consume a precomputed BlockSci analysis artifact instead of querying BlockSci.",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Also render a Markdown report next to the JSON output.",
    )
    add_detector_arguments(parser)
    add_image_arguments(parser)
    return parser.parse_args(argv)


def assemble_report(args: argparse.Namespace) -> int:
    run_dir = resolve_run_dir(args.runs_root, args.run_dir)
    analysis_dir = coinjoin_analysis_dir(run_dir)
    output_dir = report_dir(run_dir)
    baseline_path = run_dir / BASELINE_FILE
    if not baseline_path.exists():
        raise FileNotFoundError(f"Baseline coinjoin-analysis file not found: {baseline_path}")

    coinjoin_analysis_data = load_json(baseline_path)
    false_positive_txids, false_positive_sources = load_false_positive_txids(analysis_dir)
    coinjoin_analysis_data, filtered_txids = filter_coinjoin_analysis_false_positives(
        coinjoin_analysis_data,
        false_positive_txids,
    )
    coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_data)
    fill_missing_block_heights(coinjoin_analysis, load_exported_block_tx_index(run_dir))
    scenario = load_scenario(run_dir, args.scenario)
    mapping_data = None
    mapping_manifest = mappings_dir(run_dir) / "coinjoin_mappings.json"
    if args.coinjoin_type == "wasabi2" and mapping_manifest.is_file():
        mapping_data = load_json(mapping_manifest)
    emulator_data = None
    if args.mode == "emulator":
        emulator_data = build_emulator_data(run_dir, coinjoin_analysis_data, args.coinjoin_type)
    output_dir.mkdir(parents=True, exist_ok=True)
    if emulator_data is not None:
        save_json(output_dir / "emulator_data.json", emulator_data)

    min_input_count = args.min_input_count
    analysis_path = args.blocksci_analysis or (blocksci_analysis_dir(run_dir) / ARTIFACT_NAME)
    if not analysis_path.is_absolute():
        analysis_path = run_dir / analysis_path
    analysis = load_blocksci_analysis(
        analysis_path,
        run_id=run_dir.name,
        expected_parameters=blocksci_detector_parameters(args),
        mode=args.mode,
    )
    provenance = analysis.get("image_provenance")
    if provenance is None:
        provenance = (analysis.get("integration_diagnostics") or {}).get("images", {})
    first_wasabi2_block = analysis["first_wasabi2_block"]
    blocksci_records = analysis["records"]
    blocksci_skipped_txids = analysis["skipped_txids"]
    integration_diagnostics = analysis["integration_diagnostics"]
    predicted_address_clusters = analysis.get("predicted_address_clusters")
    cluster_export_error = analysis.get("cluster_export_error")
    if args.skip_clustering:
        predicted_address_clusters = None
        cluster_export_error = "Clustering was explicitly skipped during report assembly."
    output_path = output_dir / args.output_name
    previous_run_manifest = None
    if output_path.exists():
        try:
            previous_run_manifest = load_json(output_path).get("run_manifest")
        except (OSError, json.JSONDecodeError):
            previous_run_manifest = None
    report = build_report(
        run_dir,
        coinjoin_analysis,
        blocksci_records,
        args.coinjoin_type,
        scenario,
        min_input_count=min_input_count,
        first_wasabi2_block=first_wasabi2_block,
        emulator_data=emulator_data,
        predicted_address_clusters=predicted_address_clusters,
        cluster_export_error=cluster_export_error,
        blocksci_skipped_txids=blocksci_skipped_txids,
        joinmarket_detector=args.joinmarket_detector,
        joinmarket_min_base_fee=args.joinmarket_min_base_fee,
        joinmarket_percentage_fee=args.joinmarket_percentage_fee,
        joinmarket_max_depth=args.joinmarket_max_depth,
        engine=args.engine,
        blocksci_image=args.blocksci_image,
        coinjoin_analysis_image=args.coinjoin_analysis_image,
        coinjoin_emulator_image=args.coinjoin_emulator_image,
        uploader_image=args.uploader_image,
        unified_report_image=args.unified_report_image,
        blocksci_image_digest=args.blocksci_image_digest,
        coinjoin_analysis_image_digest=args.coinjoin_analysis_image_digest,
        coinjoin_emulator_image_digest=args.coinjoin_emulator_image_digest,
        # Same derivation as the diagnostics block above; passing it explicitly
        # keeps both call sites in one place instead of relying on the manifest
        # builder repeating the fallback.
        uploader_image_digest=getattr(args, "uploader_image_digest", None)
        or digest_from_reference(args.uploader_image),
        unified_report_image_digest=digest_from_reference(args.unified_report_image),
        emulator_git_commit=args.emulator_git_commit,
        previous_run_manifest=previous_run_manifest,
        integration_diagnostics=integration_diagnostics,
        mode=args.mode,
        network=args.network,
        coinjoin_mappings=mapping_data,
        image_provenance=provenance,
    )
    report["baseline_filter"] = {
        "enabled": bool(false_positive_sources),
        "sources": false_positive_sources,
        "listed_txids": len(false_positive_txids),
        "filtered_txids": filtered_txids,
        "filtered_count": len(filtered_txids),
    }
    save_json(output_path, report)
    print(f"Unified report saved to {output_path}")
    if args.markdown:
        try:
            from exporters.markdown_report import render_report, save_text
        except ImportError:  # pragma: no cover - supports direct script execution from exporters/.
            from markdown_report import render_report, save_text  # type: ignore[import-not-found, no-redef]

        markdown_path = output_path.with_suffix(".md")
        save_text(markdown_path, render_report(report))
        print(f"Markdown report saved to {markdown_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return assemble_report(parse_args(argv))
