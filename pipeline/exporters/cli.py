"""Command-line entrypoint for unified report export."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from exporters.analysis_artifact import ARTIFACT_NAME
from exporters.analysis_artifact import detector_parameters as blocksci_detector_parameters
from exporters.analysis_artifact import load_analysis as load_blocksci_analysis
from exporters.artifact_paths import (
    BASELINE_FILE,
    DUMPLINGS_SNAPSHOT_FILE,
    MAPPINGS_FILE,
    REPORT_JSON,
    SCENARIO_FILE,
    blocksci_analysis_dir,
    coinjoin_analysis_dir,
    report_dir,
)
from exporters.common import (
    JsonObject,
    digest_from_reference,
    load_json,
    save_json,
)
from exporters.emulator_data import build_emulator_data
from exporters.liquidity import build_liquidity, save_liquidity_dataset
from exporters.markdown_report import render_report, save_text
from exporters.normalization import (
    baseline_filter_summary,
    fill_missing_block_heights,
    filter_coinjoin_analysis_false_positives,
    load_exported_block_tx_index,
    load_false_positive_txids,
    normalize_coinjoin_analysis,
    restrict_baseline_to_window,
)
from exporters.parameters import add_detector_arguments, add_image_arguments
from exporters.report_builder import build_report
from exporters.report_types import BlockSciAnalysisArtifact
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


def load_previous_run_manifest(output_path: Path) -> JsonObject | None:
    """The manifest of the report about to be replaced, for the reproducibility diff."""
    if not output_path.exists():
        return None
    try:
        return load_json(output_path).get("run_manifest")
    except (OSError, ValueError):
        return None


def load_run_analysis(args: argparse.Namespace, run_dir: Path) -> BlockSciAnalysisArtifact:
    analysis_path = args.blocksci_analysis or (blocksci_analysis_dir(run_dir) / ARTIFACT_NAME)
    if not analysis_path.is_absolute():
        analysis_path = run_dir / analysis_path
    return load_blocksci_analysis(
        analysis_path,
        run_id=run_dir.name,
        expected_parameters=blocksci_detector_parameters(args),
        mode=args.mode,
    )


def add_dumplings_snapshot(coverage: JsonObject | None, run_dir: Path) -> None:
    """Name the Dumplings snapshot an external baseline was cut from."""
    snapshot_path = run_dir / DUMPLINGS_SNAPSHOT_FILE
    if coverage is not None and snapshot_path.is_file():
        snapshot = load_json(snapshot_path)
        coverage["dumplings_last_processed_height"] = snapshot.get("last_processed_height")
        coverage["dumplings_commit"] = snapshot.get("dumplings_commit")


def assemble_report(args: argparse.Namespace) -> int:
    run_dir = resolve_run_dir(args.runs_root, args.run_dir)
    output_dir = report_dir(run_dir)
    baseline_path = run_dir / BASELINE_FILE
    if not baseline_path.exists():
        raise FileNotFoundError(f"Baseline coinjoin-analysis file not found: {baseline_path}")

    false_positive_txids, false_positive_sources = load_false_positive_txids(coinjoin_analysis_dir(run_dir))
    coinjoin_analysis_data, filtered_txids = filter_coinjoin_analysis_false_positives(
        load_json(baseline_path),
        false_positive_txids,
    )
    coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_data)
    fill_missing_block_heights(coinjoin_analysis, load_exported_block_tx_index(run_dir))
    mapping_path = run_dir / MAPPINGS_FILE
    mapping_data = load_json(mapping_path) if args.coinjoin_type == "wasabi2" and mapping_path.is_file() else None
    emulator_data = None
    if args.mode == "emulator":
        emulator_data = build_emulator_data(run_dir, coinjoin_analysis_data, args.coinjoin_type)
    output_dir.mkdir(parents=True, exist_ok=True)
    if emulator_data is not None:
        save_json(output_dir / "emulator_data.json", emulator_data)

    analysis = load_run_analysis(args, run_dir)
    # Artifacts written before image_provenance existed kept it in the diagnostics.
    provenance = analysis.get("image_provenance")
    if provenance is None:
        provenance = (analysis.get("integration_diagnostics") or {}).get("images", {})
    coverage = None
    if args.mode == "external":
        coinjoin_analysis, coverage = restrict_baseline_to_window(
            coinjoin_analysis, analysis.get("scan_window"), analysis.get("baseline_heights")
        )
        add_dumplings_snapshot(coverage, run_dir)
    predicted_address_clusters = analysis.get("predicted_address_clusters")
    cluster_export_error = analysis.get("cluster_export_error")
    if args.skip_clustering:
        predicted_address_clusters = None
        cluster_export_error = "Clustering was explicitly skipped during report assembly."
    liquidity_evaluation, liquidity_dataset = build_liquidity(
        run_dir,
        emulator_data,
        coinjoin_analysis_data,
        analysis.get("mix_events"),
        analysis.get("mix_events_error")
        if "mix_events" in analysis
        else "The BlockSci analysis artifact predates liquidity event classification; rerun the analysis.",
    )
    output_path = output_dir / args.output_name
    report = build_report(
        run_dir,
        coinjoin_analysis,
        analysis["records"],
        args.coinjoin_type,
        load_scenario(run_dir, args.scenario),
        min_input_count=args.min_input_count,
        first_wasabi2_block=analysis["first_wasabi2_block"],
        emulator_data=emulator_data,
        predicted_address_clusters=predicted_address_clusters,
        cluster_export_error=cluster_export_error,
        blocksci_skipped_txids=analysis["skipped_txids"],
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
        # The uploader and report images have no digest option; a pinned
        # reference carries its own.
        uploader_image_digest=digest_from_reference(args.uploader_image),
        unified_report_image_digest=digest_from_reference(args.unified_report_image),
        emulator_git_commit=args.emulator_git_commit,
        previous_run_manifest=load_previous_run_manifest(output_path),
        integration_diagnostics=analysis["integration_diagnostics"],
        mode=args.mode,
        network=args.network,
        coinjoin_mappings=mapping_data,
        image_provenance=provenance,
        coverage=coverage,
        baseline_filter=baseline_filter_summary(false_positive_sources, false_positive_txids, filtered_txids),
        liquidity_evaluation=liquidity_evaluation,
    )
    if liquidity_dataset is not None:
        save_liquidity_dataset(output_dir, liquidity_dataset)
    save_json(output_path, report)
    print(f"Unified report saved to {output_path}")
    if args.markdown:
        markdown_path = output_path.with_suffix(".md")
        save_text(markdown_path, render_report(report))
        print(f"Markdown report saved to {markdown_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return assemble_report(parse_args(argv))
