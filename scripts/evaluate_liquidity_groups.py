#!/usr/bin/env python3
"""Evaluate a scenario's wallet groups over an existing liquidity dataset.

Runs whose report predates ``liquidity_groups`` keep their report and dataset
untouched; this reads their ``liquidity_dataset.json`` and prints the
``large_groups`` entries the report would contain now, using the groups of the
given scenario JSON (normally the regenerated catalogue copy of the run's scenario).

    python3 scripts/evaluate_liquidity_groups.py RUN/coinjoinPipeline_data/liquidity_dataset.json \\
        --scenario ../coinjoin-scenarios/fin-split-whale.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from exporters.liquidity import fin_evaluation, group_summaries
from exporters.scenario import liquidity_groups


def evaluate(dataset: dict, groups: dict[str, list[str]]) -> list[dict]:
    coinjoins = dataset["coinjoins"]
    pool = dataset["pool"]
    summaries = group_summaries(coinjoins, pool["ground_truth"], groups)
    return [
        {
            "group": name,
            **{key: value for key, value in summary.items() if key != "series"},
            "fin": fin_evaluation(coinjoins, pool["observed"], set(summary["wallets"])),
        }
        for name, summary in summaries.items()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dataset", type=Path, help="liquidity_dataset.json of the run")
    parser.add_argument("--scenario", type=Path, required=True, help="scenario JSON with liquidity_groups")
    args = parser.parse_args(argv)
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    # The scenario-lab S3 cache wraps a downloaded file in {"data": ...}.
    dataset = dataset.get("data", dataset)
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    groups = liquidity_groups(scenario)
    if not groups:
        parser.error(f"{args.scenario} declares no liquidity_groups")
    result = {
        "run_id": dataset.get("run_id"),
        "scenario": scenario.get("name"),
        "large_groups": evaluate(dataset, groups),
    }
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
