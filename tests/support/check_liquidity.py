#!/usr/bin/env python3
"""End-to-end checks of the liquidity evaluation a full run wrote next to its report.

    check_liquidity.py <report-dir> [--require-blocksci] [--require-postmix] [--exact-baseline]

<report-dir> holds unified_report.json, liquidity_dataset.json and
liquidity_events.csv (coinjoinPipeline_data/ of a run, or a download of it).
Every run must have an available evaluation with fresh inflow and a dataset
that matches the report. The options add what a scenario or image mode makes
certain:

  --require-blocksci  BlockSci classified the same CoinJoins (needs a BlockSci
                      image with classify_coinjoin_mix_events).
  --require-postmix   the scenario scheduled postmix spends: each was sent and
                      confirmed, and value left the mix in the ground truth and
                      in every analyzer.
  --exact-baseline    coinjoin-analysis classified every input and output as
                      the ground truth did, so both liquidity curves coincide.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"FAIL: liquidity: {message}")


def load(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size == 0:
        fail(f"missing or empty {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def check(report_dir: Path, args: argparse.Namespace) -> str:
    report = load(report_dir / "unified_report.json")
    liquidity = report.get("liquidity_evaluation")
    if not isinstance(liquidity, dict):
        fail("unified_report.json has no liquidity_evaluation section")
    if liquidity.get("available") is not True:
        fail(f"evaluation unavailable: {liquidity.get('reason')!r}")
    ground_truth = liquidity.get("ground_truth") or {}
    if not liquidity.get("coinjoins") or not ground_truth.get("entered_sats"):
        fail(f"no coinjoin inflow in the ground truth: {liquidity.get('coinjoins')!r} coinjoins, {ground_truth!r}")

    dataset = load(report_dir / "liquidity_dataset.json")
    if len(dataset.get("coinjoins") or []) != liquidity["coinjoins"]:
        fail(f"dataset has {len(dataset.get('coinjoins') or [])} coinjoins, report {liquidity['coinjoins']}")
    events_path = report_dir / "liquidity_events.csv"
    if not events_path.is_file():
        fail(f"missing {events_path}")
    with events_path.open(encoding="utf-8", newline="") as file:
        rows = sum(1 for _ in csv.DictReader(file))
    agreement = liquidity.get("classification_agreement") or {}
    if rows != agreement.get("classified"):
        fail(f"liquidity_events.csv has {rows} rows, report classified {agreement.get('classified')!r}")

    analyzers = {"coinjoin-analysis": liquidity.get("observed") or {}}
    blocksci = liquidity.get("blocksci")
    if args.require_blocksci:
        if not isinstance(blocksci, dict):
            fail("no BlockSci classification (exporter predates BlockSci liquidity)")
        if blocksci.get("available") is not True:
            fail(f"BlockSci classification unavailable: {blocksci.get('reason')!r}")
        analyzers["BlockSci"] = blocksci

    if args.exact_baseline:
        if agreement.get("agreement_rate") != 1.0:
            disagreements = {
                key: count
                for key, count in (agreement.get("confusion") or {}).items()
                if key.split(":", 1)[1].split("->")[0] != key.split("->")[1]
            }
            fail(f"coinjoin-analysis disagrees with the ground truth: {disagreements}")
        if liquidity.get("interim_max_abs_difference_sats") != 0:
            fail(f"coinjoin-analysis curve differs by {liquidity.get('interim_max_abs_difference_sats')} sats")

    if args.require_postmix:
        spends = liquidity.get("postmix_spends") or {}
        statuses = spends.get("by_status") or {}
        scheduled = spends.get("scheduled") or 0
        if not scheduled or statuses.get("sent") != scheduled:
            fail(f"not every scheduled postmix spend was sent: {spends!r}")
        if spends.get("confirmed_postmix") != scheduled:
            fail(
                f"only {spends.get('confirmed_postmix')!r} of {scheduled} spends confirmed as spends of coinjoin outputs"
            )
        if not ground_truth.get("left_sats"):
            fail("no value left the mix in the ground truth")
        for name, section in analyzers.items():
            if not section.get("left_sats"):
                fail(f"{name} classified no output as MIX_LEAVE")

    summary = (
        f"{liquidity['coinjoins']} coinjoins, {rows} events, "
        f"entered={ground_truth.get('entered_sats')} left={ground_truth.get('left_sats')}, "
        f"coinjoin-analysis agreement={agreement.get('agreement_rate')}"
    )
    if isinstance(blocksci, dict) and blocksci.get("available"):
        summary += f", BlockSci agreement={(blocksci.get('classification_agreement') or {}).get('agreement_rate')}"
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report_dir", type=Path)
    parser.add_argument("--require-blocksci", action="store_true")
    parser.add_argument("--require-postmix", action="store_true")
    parser.add_argument("--exact-baseline", action="store_true")
    args = parser.parse_args()
    print(f"PASS: liquidity: {check(args.report_dir, args)}")


if __name__ == "__main__":
    sys.exit(main())
