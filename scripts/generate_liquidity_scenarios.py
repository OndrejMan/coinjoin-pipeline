"""Generate the Wasabi liquidity scenarios from the overactive-local background.

Each scenario keeps the 15 overactive-local wallets as a pool that remixes for the
whole run, adds a steady trickle of fresh inflow and small postmix spends, and puts
one or two large wallets on top whose entry, mixing and spending out should show as
a "fin" in the cumulative pool liquidity (Svenda et al., PETS 2026, section 5.3).

    python3 scripts/generate_liquidity_scenarios.py scenarios/overactive-local.json scenarios
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

ROUNDS = 24
# The coordinator's MaxSuggestedAmountBase is 0.1 BTC; smaller coins join regular rounds.
LARGE_COIN_SATS = 9_900_000
SMALL_COIN_SATS = 990_000
LARGE_WALLET_COINS = 50


def background(base: dict[str, object]) -> list[dict[str, object]]:
    wallets = []
    for index, wallet in enumerate(base["wallets"]):
        copied = copy.deepcopy(wallet)
        # Keep remixing for the whole run so every round reaches the 12-input minimum.
        copied["wasabi"]["anon_score_target"] = 1000000 if index == 0 else 50
        wallets.append(copied)
    # Steady fresh inflow and small postmix spends form the baseline a large wallet stands out from.
    for index, rounds in ((3, 4), (6, 10), (9, 16), (12, 20)):
        wallets[index]["funds"].append({"value": 10_000_000, "delay_rounds": rounds})
    for index, rounds in ((1, 8), (7, 12), (11, 16), (13, 20)):
        wallets[index]["wasabi"]["postmix_spends"] = [{"value": 5_000_000, "delay_rounds": rounds}]
    return wallets


def large_wallet(coin_sats: int, enter: int, spend_rounds: tuple[int, ...], stop: int) -> dict[str, object]:
    """A wallet entering at round ``enter``, spending a fifth at each round, and the rest at ``stop``."""
    spends: list[dict[str, object]] = [
        {"value": LARGE_WALLET_COINS * coin_sats // 5, "delay_rounds": rounds} for rounds in spend_rounds
    ]
    spends.append({"all": True, "delay_rounds": stop})
    return {
        "funds": [coin_sats] * LARGE_WALLET_COINS,
        "delay_blocks": None,
        "delay_rounds": enter,
        "stop_blocks": None,
        "stop_rounds": stop,
        "version": None,
        "wasabi": {"anon_score_target": 10, "redcoin_isolation": None, "skip_rounds": None, "postmix_spends": spends},
        "joinmarket": None,
    }


VARIANTS = {
    # 4.95 BTC against ~4.45 BTC of background: larger than all other wallets together.
    "liquidity-large-wallet": [large_wallet(LARGE_COIN_SATS, 6, (12, 14, 16), 18)],
    # Control: the same behaviour with 0.495 BTC stays well below the pool.
    "liquidity-small-wallet": [large_wallet(SMALL_COIN_SATS, 6, (12, 14, 16), 18)],
    # Two overlapping large wallets: does the curve separate them?
    "liquidity-two-large-wallets": [
        large_wallet(LARGE_COIN_SATS, 4, (10, 12), 14),
        large_wallet(LARGE_COIN_SATS, 9, (16, 18), 20),
    ],
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", type=Path, help="overactive-local.json to take the background wallets from")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    base = json.loads(args.base.read_text(encoding="utf-8"))
    for name, extra in VARIANTS.items():
        scenario = {key: value for key, value in base.items() if key != "wallets"}
        scenario.update({"name": name, "rounds": ROUNDS, "wallets": background(base) + extra})
        path = args.output_dir / f"{name}.json"
        path.write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
