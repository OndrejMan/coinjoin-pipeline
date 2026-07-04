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
) -> JsonObject:
    wallet_names = coinjoin_analysis_wallet_names(coinjoin_analysis)
    coinjoin_analysis_input_sats = sum(tx.get("total_input_sats", 0) for tx in coinjoin_analysis.values())
    scenario_wallet_count = scenario.get("wallet_count") if scenario else None
    scenario_initial_funds = scenario.get("total_initial_funds_sats") if scenario else None

    return {
        "scenario_wallet_count": scenario_wallet_count,
        "coinjoin_analysis_wallet_count": len(wallet_names),
        "wallet_count_matches": scenario_wallet_count == len(wallet_names) if scenario else None,
        "coinjoin_analysis_input_sats": coinjoin_analysis_input_sats,
        "scenario_initial_funds_sats": scenario_initial_funds,
        "input_sats_within_scenario_funds": (
            coinjoin_analysis_input_sats <= scenario_initial_funds if scenario_initial_funds is not None else None
        ),
        "per_wallet_observed_counts": per_wallet_observed_counts(coinjoin_analysis),
    }
