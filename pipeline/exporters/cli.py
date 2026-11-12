"""Command-line entrypoint for unified report export."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from exporters.analysis_artifact import ARTIFACT_NAME
from exporters.analysis_artifact import load_analysis as load_blocksci_analysis
from exporters.artifact_paths import (
    BASELINE_FILE,
    REPORT_JSON,
    blocksci_analysis_dir,
    report_dir,
)
from exporters.common import (
    load_json,
    save_json,
)
from exporters.emulator_data import build_emulator_data
from exporters.markdown_report import render_report, save_text
from exporters.normalization import (
    fill_missing_block_heights,
    load_exported_block_tx_index,
    normalize_coinjoin_analysis,
)
from exporters.parameters import Detector, ReportImages, add_detector_arguments, add_image_arguments
from exporters.report_builder import build_report
from exporters.report_types import BlockSciAnalysisArtifact
from exporters.scenario import load_scenario


def resolve_run_dir(runs_root: Path, run_dir_arg: str | Path) -> Path:
    run_dir = Path(run_dir_arg).expanduser()
    if not run_dir.is_absolute():
        run_dir = runs_root.expanduser() / run_dir
    return run_dir.resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a unified BlockSci-vs-emulator JSON report.")
    parser.add_argument("--config", type=Path, help="BlockSci config; defaults to the selected run config.")
    parser.add_argument("--runs-root", type=Path, default=Path("/runs/emulation/logs"))
    parser.add_argument("--run-dir", required=True, help="Run folder path or name under --runs-root.")
    parser.add_argument("--engine", default=os.environ.get("COINJOIN_ENGINE"))
    parser.add_argument("--output-name", default=REPORT_JSON)
    parser.add_argument("--emulator-git-commit", default=os.environ.get("COINJOIN_EMULATOR_GIT_COMMIT"))
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


def load_run_analysis(args: argparse.Namespace, run_dir: Path) -> BlockSciAnalysisArtifact:
    analysis_path = args.blocksci_analysis or (blocksci_analysis_dir(run_dir) / ARTIFACT_NAME)
    if not analysis_path.is_absolute():
        analysis_path = run_dir / analysis_path
    return load_blocksci_analysis(
        analysis_path,
        run_id=run_dir.name,
        expected_parameters=Detector.from_args(args).artifact_parameters(),
    )


def assemble_report(args: argparse.Namespace) -> int:
    run_dir = resolve_run_dir(args.runs_root, args.run_dir)
    output_dir = report_dir(run_dir)
    baseline_path = run_dir / BASELINE_FILE
    if not baseline_path.exists():
        raise FileNotFoundError(f"Baseline coinjoin-analysis file not found: {baseline_path}")

    coinjoin_analysis_data = load_json(baseline_path)
    coinjoin_analysis = normalize_coinjoin_analysis(coinjoin_analysis_data)
    fill_missing_block_heights(coinjoin_analysis, load_exported_block_tx_index(run_dir))
    emulator_data = build_emulator_data(run_dir, coinjoin_analysis_data, args.coinjoin_type)
    output_dir.mkdir(parents=True, exist_ok=True)
    save_json(output_dir / "emulator_data.json", emulator_data)

    analysis = load_run_analysis(args, run_dir)
    scenario = load_scenario(run_dir)
    output_path = output_dir / args.output_name
    report = build_report(
        run_dir,
        coinjoin_analysis,
        analysis["records"],
        Detector.from_args(args, analysis["first_wasabi2_block"]),
        scenario,
        emulator_data=emulator_data,
        engine=args.engine,
        images=ReportImages.from_args(args),
        emulator_git_commit=args.emulator_git_commit,
        image_provenance=analysis.get("image_provenance"),
    )
    save_json(output_path, report)
    print(f"Unified report saved to {output_path}")
    if args.markdown:
        markdown_path = output_path.with_suffix(".md")
        save_text(markdown_path, render_report(report))
        print(f"Markdown report saved to {markdown_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return assemble_report(parse_args(argv))
