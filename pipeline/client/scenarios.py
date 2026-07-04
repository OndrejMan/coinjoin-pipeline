"""Pure-Python scenario discovery and validation for researcher commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

SCENARIOS_ROOT = Path(
    os.environ.get("SCENARIOS_ROOT", Path(__file__).resolve().parents[2] / "bitcoinAnalysis" / "scenarios")
).expanduser()


def resolve_scenario(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    named = SCENARIOS_ROOT / value
    if named.suffix != ".json":
        named = named.with_suffix(".json")
    if not named.is_file():
        raise ValueError(f"Scenario not found: {value}")
    return named.resolve()


def load_scenario(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"Scenario must be a JSON object: {path}")
    return data


def validate_scenario(path: Path, engine: str) -> dict[str, object]:
    data = load_scenario(path)
    for field in ("name", "rounds", "blocks", "default_version", "wallets"):
        if field not in data:
            raise ValueError(f"Scenario {path} is missing required field: {field}")
    if not isinstance(data["name"], str) or not data["name"]:
        raise ValueError("Scenario name must be a non-empty string")
    for field in ("rounds", "blocks"):
        value = data[field]
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"Scenario {field} must be a non-negative integer")
    wallets = data["wallets"]
    if not isinstance(wallets, list) or not wallets:
        raise ValueError("Scenario wallets must be a non-empty list")

    makers = takers = wasabi_wallets = 0
    for index, wallet in enumerate(wallets):
        if not isinstance(wallet, dict):
            raise ValueError(f"Wallet {index} must be an object")
        funds = wallet.get("funds")
        if not isinstance(funds, list) or not funds or any(not isinstance(value, int) or value <= 0 for value in funds):
            raise ValueError(f"Wallet {index} funds must be a non-empty list of positive integers")
        joinmarket = wallet.get("joinmarket")
        if engine == "joinmarket":
            if not isinstance(joinmarket, dict) or joinmarket.get("role") not in {"maker", "taker"}:
                raise ValueError(f"JoinMarket wallet {index} must define role 'maker' or 'taker'")
            makers += joinmarket.get("role") == "maker"
            takers += joinmarket.get("role") == "taker"
        elif joinmarket not in (None, {}):
            raise ValueError(f"Wasabi scenario cannot contain JoinMarket configuration (wallet {index})")
        if isinstance(wallet.get("wasabi"), dict):
            wasabi_wallets += 1
    if engine == "joinmarket" and (makers == 0 or takers == 0):
        raise ValueError("JoinMarket scenario requires at least one maker and one taker")

    return {
        "path": str(path),
        "name": data["name"],
        "engine": engine,
        "rounds": data["rounds"],
        "blocks": data["blocks"],
        "wallet_count": len(wallets),
        "total_funds_sats": sum(sum(wallet["funds"]) for wallet in wallets),
        "makers": makers,
        "takers": takers,
        "wasabi_wallets": wasabi_wallets,
    }


def packaged_scenarios(engine: str | None = None) -> list[dict[str, object]]:
    results = []
    for path in sorted(SCENARIOS_ROOT.glob("*.json")):
        data = load_scenario(path)
        inferred = "joinmarket" if data.get("default_version") == "joinmarket" else "wasabi"
        if engine is None or inferred == engine:
            results.append(validate_scenario(path, inferred))
    return results
