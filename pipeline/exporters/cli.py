"""Command-line entrypoint for unified report export."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from exporters.artifact_paths import coinjoin_analysis_dir, mappings_dir, report_dir
from exporters.blocksci_export import blocksci, export_blocksci_cluster_assignments, export_blocksci_records
from exporters.common import (
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    load_json,
    save_json,
)
from exporters.emulator_data import build_emulator_data
from exporters.integration_diagnostics import build_integration_diagnostics
from exporters.normalization import (
    fill_missing_block_heights,
    filter_coinjoin_analysis_false_positives,
    load_exported_block_tx_index,
    load_false_positive_txids,
    load_first_wasabi2_block,
    normalize_coinjoin_analysis,
)
from exporters.report_builder import build_report
from exporters.scenario import load_scenario


def find_latest_run_dir(runs_root: Path) -> Path:
    candidates = [
        child
        for child in runs_root.iterdir()
        if child.is_dir()
        and (child / "coinjoin_emulator_data" / "scenario.json").exists()
    ]
    if not candidates:
        raise FileNotFoundError(f"No emulation run folders found under {runs_root}")
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    print(f"[WARN] No --run-dir provided; using newest run folder: {latest}", file=sys.stderr)
    return latest


def resolve_run_dir(runs_root: Path, run_dir_arg: str | None) -> Path:
    if run_dir_arg is None or not run_dir_arg.strip():
        return find_latest_run_dir(runs_root)
    run_dir = Path(run_dir_arg)
    if not run_dir.is_absolute():
        run_dir = runs_root / run_dir
    return run_dir


def parse_min_input_count(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"", "none", "null", "default"}:
        return None
    parsed = int(normalized)
    return parsed if parsed > 0 else None


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
    parser.add_argument("--coinjoin-type", default="wasabi2")
    parser.add_argument(
        "--min-input-count",
        default="1",
        help="Override BlockSci detector min input count; use 'default' for BlockSci's default.",
    )
    parser.add_argument("--test-values", action="store_true", help="Use BlockSci test heuristic thresholds.")
    parser.add_argument(
        "--joinmarket-detector",
        choices=("possible", "definite"),
        default=DEFAULT_JOINMARKET_DETECTOR,
        help="JoinMarket detector to use for --coinjoin-type joinmarket.",
    )
    parser.add_argument(
        "--joinmarket-min-base-fee",
        type=int,
        default=DEFAULT_JOINMARKET_MIN_BASE_FEE,
        help="Minimum base fee passed to the BlockSci JoinMarket detector.",
    )
    parser.add_argument(
        "--joinmarket-percentage-fee",
        type=float,
        default=DEFAULT_JOINMARKET_PERCENTAGE_FEE,
        help="Percentage fee passed to the BlockSci JoinMarket detector.",
    )
    parser.add_argument(
        "--joinmarket-max-depth",
        type=int,
        default=DEFAULT_JOINMARKET_MAX_DEPTH,
        help="Maximum subset-search depth passed to the BlockSci JoinMarket detector.",
    )
    parser.add_argument("--output-name", default="unified_report.json")
    parser.add_argument("--blocksci-image", default=os.environ.get("BLOCKSCI_IMAGE"))
    parser.add_argument("--coinjoin-analysis-image", default=os.environ.get("COINJOIN_ANALYSIS_IMAGE"))
    parser.add_argument(
        "--coinjoin-emulator-image",
        default=os.environ.get("COINJOIN_EMULATOR_IMAGE") or os.environ.get("EMULATOR_IMAGE"),
    )
    parser.add_argument("--wrapper-image", default=os.environ.get("WRAPPER_IMAGE"))
    parser.add_argument("--blocksci-image-digest", default=os.environ.get("BLOCKSCI_IMAGE_DIGEST"))
    parser.add_argument("--blocksci-image-id", default=os.environ.get("BLOCKSCI_IMAGE_ID"))
    parser.add_argument(
        "--coinjoin-analysis-image-digest",
        default=os.environ.get("COINJOIN_ANALYSIS_IMAGE_DIGEST"),
    )
    parser.add_argument("--coinjoin-analysis-image-id", default=os.environ.get("COINJOIN_ANALYSIS_IMAGE_ID"))
    parser.add_argument(
        "--coinjoin-emulator-image-digest",
        default=os.environ.get("COINJOIN_EMULATOR_IMAGE_DIGEST") or os.environ.get("EMULATOR_IMAGE_DIGEST"),
    )
    parser.add_argument(
        "--coinjoin-emulator-image-id",
        default=os.environ.get("COINJOIN_EMULATOR_IMAGE_ID") or os.environ.get("EMULATOR_IMAGE_ID"),
    )
    parser.add_argument("--wrapper-image-digest", default=os.environ.get("WRAPPER_IMAGE_DIGEST"))
    parser.add_argument("--wrapper-image-id", default=os.environ.get("WRAPPER_IMAGE_ID"))
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
        "--markdown",
        action="store_true",
        help="Also render a Markdown report next to the JSON output.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = resolve_run_dir(args.runs_root, args.run_dir)
    analysis_dir = coinjoin_analysis_dir(run_dir)
    output_dir = report_dir(run_dir)
    config_path = args.config or run_dir / "blocksci_data" / "config.json"
    truth_path = analysis_dir / "coinjoin_tx_info.json"
    if not truth_path.exists():
        raise FileNotFoundError(f"Ground-truth CoinJoin file not found: {truth_path}")

    coinjoin_analysis_data = load_json(truth_path)
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

    min_input_count = parse_min_input_count(args.min_input_count)
    first_wasabi2_block = load_first_wasabi2_block(config_path)

    blocksci.heuristics.set_test_values_enabled(args.test_values)
    blocksci_records, blocksci_skipped_txids = export_blocksci_records(
        config_path,
        args.coinjoin_type,
        min_input_count,
        joinmarket_detector=args.joinmarket_detector,
        joinmarket_min_base_fee=args.joinmarket_min_base_fee,
        joinmarket_percentage_fee=args.joinmarket_percentage_fee,
        joinmarket_max_depth=args.joinmarket_max_depth,
    )
    integration_diagnostics = None
    if args.mode == "emulator":
        integration_diagnostics = build_integration_diagnostics(
            run_dir,
            config_path,
            blocksci,
            blocksci_records,
            args.coinjoin_type,
            {
                "blocksci": args.blocksci_image,
                "coinjoin_analysis": args.coinjoin_analysis_image,
                "coinjoin_emulator": args.coinjoin_emulator_image,
                "wrapper": args.wrapper_image,
            },
            image_ids={
                "blocksci": args.blocksci_image_id,
                "coinjoin_analysis": args.coinjoin_analysis_image_id,
                "coinjoin_emulator": args.coinjoin_emulator_image_id,
                "wrapper": args.wrapper_image_id,
            },
            image_digests={
                "blocksci": args.blocksci_image_digest,
                "coinjoin_analysis": args.coinjoin_analysis_image_digest,
                "coinjoin_emulator": args.coinjoin_emulator_image_digest,
                "wrapper": args.wrapper_image_digest,
            },
            joinmarket_detector=args.joinmarket_detector,
            joinmarket_min_base_fee=args.joinmarket_min_base_fee,
            joinmarket_percentage_fee=args.joinmarket_percentage_fee,
            joinmarket_max_depth=args.joinmarket_max_depth,
        )
    predicted_address_clusters = None
    cluster_export_error = None
    if not args.skip_clustering:
        cluster_output_dir = args.cluster_output_dir or (
            config_path.parent / "clustering" / f"{args.coinjoin_type}_emulator_report"
        )
        predicted_address_clusters, cluster_export_error = export_blocksci_cluster_assignments(
            config_path,
            emulator_data,
            args.coinjoin_type,
            cluster_output_dir,
        )
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
        test_values=args.test_values,
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
        wrapper_image=args.wrapper_image,
        blocksci_image_digest=args.blocksci_image_digest,
        coinjoin_analysis_image_digest=args.coinjoin_analysis_image_digest,
        coinjoin_emulator_image_digest=args.coinjoin_emulator_image_digest,
        wrapper_image_digest=args.wrapper_image_digest,
        emulator_git_commit=args.emulator_git_commit,
        previous_run_manifest=previous_run_manifest,
        integration_diagnostics=integration_diagnostics,
        mode=args.mode,
        network=args.network,
        coinjoin_mappings=mapping_data,
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
