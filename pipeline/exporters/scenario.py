"""Scenario normalization and consistency checks."""

from __future__ import annotations

from pathlib import Path

from exporters.artifact_paths import emulator_dir
from exporters.common import JsonObject, coerce_sats, load_json, sha256_json


def wallet_name(index: int) -> str:
    return f"wallet-{index:03}"


def normalize_scenario(scenario: JsonObject, source_path: Path) -> JsonObject:
    default_version = scenario.get("default_version")
    wallets = []
    total_initial_funds = 0

    for index, wallet in enumerate(scenario.get("wallets", [])):
        funds = [coerce_sats(value) or 0 for value in wallet.get("funds", [])]
        total_funds = sum(funds)
        total_initial_funds += total_funds

        normalized_wallet: JsonObject = {
            "wallet_name": wallet_name(index),
            "funds": funds,
            "total_funds_sats": total_funds,
            "version": wallet.get("version") or default_version,
        }
        if "wasabi" in wallet:
            normalized_wallet["wasabi"] = wallet["wasabi"]
        if "joinmarket" in wallet:
            normalized_wallet["joinmarket"] = wallet["joinmarket"]
        wallets.append(normalized_wallet)

    return {
        "source": str(source_path),
        "sha256": sha256_json(scenario),
        "name": scenario.get("name"),
        "rounds": scenario.get("rounds"),
        "blocks": scenario.get("blocks"),
        "default_version": default_version,
        "wallet_count": len(wallets),
        "total_initial_funds_sats": total_initial_funds,
        "wallets": wallets,
    }


def load_scenario(run_dir: Path, fallback_path: Path | None) -> JsonObject | None:
    run_scenario_path = emulator_dir(run_dir) / "scenario.json"
    if run_scenario_path.exists():
        return normalize_scenario(load_json(run_scenario_path), run_scenario_path)

    if fallback_path is not None and fallback_path.exists():
        return normalize_scenario(load_json(fallback_path), fallback_path)

    return None


def coinjoin_analysis_wallet_names(coinjoin_analysis: dict[str, JsonObject]) -> set[str]:
    names = set()
    for tx in coinjoin_analysis.values():
        for side in ("inputs", "outputs"):
            for item in tx.get(side, []):
                wallet = item.get("wallet_name")
                if wallet and wallet.startswith("wallet-"):
                    names.add(wallet)
    return names


def per_wallet_observed_counts(coinjoin_analysis: dict[str, JsonObject]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for tx in coinjoin_analysis.values():
        for item in tx.get("inputs", []):
            wallet = item.get("wallet_name")
            if not wallet:
                continue
            counts.setdefault(wallet, {"input_count": 0, "output_count": 0})
            counts[wallet]["input_count"] += 1
        for item in tx.get("outputs", []):
            wallet = item.get("wallet_name")
            if not wallet:
                continue
            counts.setdefault(wallet, {"input_count": 0, "output_count": 0})
            counts[wallet]["output_count"] += 1

    return {wallet: counts[wallet] for wallet in sorted(counts)}


def build_scenario_checks(
    scenario: JsonObject | None,
    coinjoin_analysis: dict[str, JsonObject],
    coinjoin_type: str | None = None,
) -> JsonObject:
    wallet_names = coinjoin_analysis_wallet_names(coinjoin_analysis)
    input_sats_per_coinjoin = [tx.get("total_input_sats", 0) for tx in coinjoin_analysis.values()]
    coinjoin_analysis_input_sats = sum(input_sats_per_coinjoin)
    max_coinjoin_input_sats = max(input_sats_per_coinjoin, default=0)
    scenario_wallet_count = scenario.get("wallet_count") if scenario else None
    scenario_initial_funds = scenario.get("total_initial_funds_sats") if scenario else None

    # How many of the scenario's wallets must show up in the CoinJoins depends on
    # the protocol. Wasabi registers every wallet over the run, so the counts have
    # to match exactly. A JoinMarket taker picks a fixed number of counterparties
    # per round (4 by default), so makers that were never chosen never appear —
    # `default-joinmarket` has 2 takers and 8 makers over 3 rounds and legitimately
    # lands anywhere from 5 to 10 observed wallets. Demanding equality there
    # reported a false negative on nearly every run.
    observed_wallet_count = len(wallet_names)
    if not scenario:
        wallet_count_rule = None
        wallet_count_matches = None
    elif coinjoin_type == "joinmarket":
        wallet_count_rule = "subset"
        wallet_count_matches = (
            0 < observed_wallet_count <= scenario_wallet_count
            if scenario_wallet_count is not None
            else None
        )
    else:
        wallet_count_rule = "exact"
        wallet_count_matches = scenario_wallet_count == observed_wallet_count

    # The funds bound is per CoinJoin, not over their sum: a multi-round scenario
    # remixes the same coins, so every round after the first counts the same
    # satoshis again and the sum legitimately exceeds what was funded once (a
    # 10-round overactive-local run lands around 1.5-2.3x). A single CoinJoin
    # consuming more than the scenario ever funded is the real impossibility, and
    # that is what this checks. The aggregate stays as `coinjoin_analysis_input_sats`
    # with its remix ratio, both informational.
    return {
        "scenario_wallet_count": scenario_wallet_count,
        "coinjoin_analysis_wallet_count": observed_wallet_count,
        "wallet_count_rule": wallet_count_rule,
        "wallet_count_matches": wallet_count_matches,
        "coinjoin_analysis_input_sats": coinjoin_analysis_input_sats,
        "max_coinjoin_input_sats": max_coinjoin_input_sats,
        "scenario_initial_funds_sats": scenario_initial_funds,
        "input_sats_within_scenario_funds": (
            max_coinjoin_input_sats <= scenario_initial_funds if scenario_initial_funds is not None else None
        ),
        "coinjoin_input_sats_to_funds_ratio": (
            round(coinjoin_analysis_input_sats / scenario_initial_funds, 3)
            if scenario_initial_funds
            else None
        ),
        "per_wallet_observed_counts": per_wallet_observed_counts(coinjoin_analysis),
    }
