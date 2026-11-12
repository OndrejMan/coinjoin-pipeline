"""Scenario normalization and consistency checks."""

from __future__ import annotations

from pathlib import Path

from exporters.artifact_paths import SCENARIO_FILE
from exporters.common import JsonObject, coerce_sats, load_json, sha256_json


def wallet_name(index: int) -> str:
    return f"wallet-{index:03}"


def normalize_scenario(scenario: JsonObject, source_path: Path) -> JsonObject:
    default_version = scenario.get("default_version")
    wallets = []
    total_initial_funds = 0

    for index, wallet in enumerate(scenario.get("wallets", [])):
        # A delayed fund is an object carrying its amount in "value".
        funds = [
            coerce_sats(fund.get("value") if isinstance(fund, dict) else fund) or 0 for fund in wallet.get("funds", [])
        ]
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


def load_scenario(run_dir: Path) -> JsonObject | None:
    """The scenario the emulator stored with the run, if any."""
    run_scenario_path = run_dir / SCENARIO_FILE
    if not run_scenario_path.exists():
        return None
    return normalize_scenario(load_json(run_scenario_path), run_scenario_path)
