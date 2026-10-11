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
# A wallet registers at most ten inputs a round, its own remixes included, so 50 coins
# enter over several rounds; only coins split across wallets enter at once.
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


def mixing_wallet(
    funds: list[object], enter: int, spends: list[dict[str, object]] | None, stop: int | None
) -> dict[str, object]:
    """A wallet mixing from round ``enter`` to ``stop`` that spends its mixed coins as ``spends`` say."""
    wasabi: dict[str, object] = {"anon_score_target": 10, "redcoin_isolation": None, "skip_rounds": None}
    if spends:
        wasabi["postmix_spends"] = spends
    return {
        "funds": funds,
        "delay_blocks": None,
        "delay_rounds": enter,
        "stop_blocks": None,
        "stop_rounds": stop,
        "version": None,
        "wasabi": wasabi,
        "joinmarket": None,
    }


def coins(count: int, arrive: int | None = None, coin_sats: int = LARGE_COIN_SATS) -> list[object]:
    """``count`` coins funded before the run, or arriving at round ``arrive``."""
    return [coin_sats if arrive is None else {"value": coin_sats, "delay_rounds": arrive} for _ in range(count)]


def parts(total_sats: int, rounds: tuple[int, ...], share: int) -> list[dict[str, object]]:
    """Spend ``total_sats // share`` at each of ``rounds``; Wasabi pays whole coins, so slightly more leaves."""
    return [{"value": total_sats // share, "delay_rounds": round_} for round_ in rounds]


def leave_all(round_: int) -> dict[str, object]:
    """Spend every confirmed mixed coin; coins that have not mixed yet stay in the wallet."""
    return {"all": True, "delay_rounds": round_}


def large_wallet(
    coin_sats: int, enter: int, spend_rounds: tuple[int, ...], stop: int, count: int = LARGE_WALLET_COINS
) -> dict[str, object]:
    """A wallet entering at round ``enter``, spending a fifth at each round, and the rest at ``stop``."""
    spends = parts(count * coin_sats, spend_rounds, 5) + [leave_all(stop)]
    return mixing_wallet(coins(count, coin_sats=coin_sats), enter, spends, stop)


def split_wallets(wallets: int, enter: int, spend_rounds: tuple[int, ...], stop: int) -> list[dict[str, object]]:
    """The large wallet's 50 coins spread over ``wallets`` wallets that act in concert."""
    return [
        large_wallet(LARGE_COIN_SATS, enter, spend_rounds, stop, LARGE_WALLET_COINS // wallets) for _ in range(wallets)
    ]


def churn_wallet(enter: int, leave: int) -> dict[str, object]:
    """About 1 BTC that enters, mixes briefly and leaves: background noise around a fin."""
    return mixing_wallet(coins(10), enter, [leave_all(leave)], leave)


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
    # Fin shapes. Each keeps the background above and changes one thing about the large wallet.
    # Rectangle: climbs over the first rounds, holds, and leaves in a single cliff at round 16.
    "fin-cliff": [mixing_wallet(coins(50), 4, [leave_all(16)], 16)],
    # Long tail: an eighth leaves every second round, so the descent spans 14 rounds.
    "fin-long-tail": [
        mixing_wallet(coins(50), 2, parts(50 * LARGE_COIN_SATS, (8, 10, 12, 14, 16, 18, 20), 8) + [leave_all(22)], 22)
    ],
    # Reverse fin: ten coins arrive every second round, then everything leaves at once.
    "fin-ramp": [
        mixing_wallet([coin for arrive in (2, 4, 6, 8, 10) for coin in coins(10, arrive)], 2, [leave_all(16)], 16)
    ],
    # Spike: five coordinated wallets enter together and leave within the three-coinjoin window.
    "fin-spike": split_wallets(5, 6, (), 9),
    # The large-wallet schedule split over five wallets: the same fin, but no single wallet
    # reaches the report's 50 % share of the pool.
    "fin-split-whale": split_wallets(5, 6, (12, 14, 16), 18),
    # No fin: the wallet stops mixing at round 12 but never spends, so its coins stay in the pool.
    "fin-plateau": [mixing_wallet(coins(50), 4, None, 12)],
    # Two separate fins from two 2.97 BTC wallets.
    "fin-sequential": [
        mixing_wallet(coins(30), 1, parts(30 * LARGE_COIN_SATS, (6,), 2) + [leave_all(9)], 9),
        mixing_wallet(coins(30), 12, parts(30 * LARGE_COIN_SATS, (17,), 2) + [leave_all(20)], 20),
    ],
    # A 1.98 BTC fin inside the descent-free middle of a 4.95 BTC fin.
    "fin-nested": [
        large_wallet(LARGE_COIN_SATS, 2, (14, 16, 18), 20),
        mixing_wallet(coins(20), 8, [leave_all(13)], 13),
    ],
    # Between the small and the large wallet: 2.475 BTC, about half of the background.
    "fin-medium-wallet": [large_wallet(LARGE_COIN_SATS, 6, (12, 14, 16), 18, 25)],
    # The large-wallet fin with four ~1 BTC wallets entering and leaving during its descent,
    # so outputs leaving in the descending window are no longer mostly the large wallet's.
    "fin-noisy-background": [
        large_wallet(LARGE_COIN_SATS, 6, (12, 14, 16), 18),
        churn_wallet(8, 12),
        churn_wallet(11, 15),
        churn_wallet(14, 18),
        churn_wallet(17, 21),
    ],
    # One wallet, two fins: it leaves at round 9, is funded again at round 12 and leaves at 20.
    "fin-double": [mixing_wallet(coins(30) + coins(30, 12), 2, [leave_all(9), leave_all(20)], 20)],
}

# Display-only catalogue metadata. Keep it next to the generated behaviour so a
# regeneration cannot inherit the background scenario's description.
DESCRIPTIONS = {
    "liquidity-large-wallet": "4.95 BTC · one large entry",
    "liquidity-small-wallet": "0.495 BTC · control scenario",
    "liquidity-two-large-wallets": "Two overlapping entries",
    "fin-cliff": "Holds, then leaves at once",
    "fin-long-tail": "Leaves in eighths over 14 rounds",
    "fin-ramp": "Slow inflow, leaves at once",
    "fin-spike": "5 wallets, leave within 3 coinjoins",
    "fin-split-whale": "4.95 BTC split over 5 wallets",
    "fin-plateau": "Stop without spending: a step, no descent",
    "fin-sequential": "Two separate fins",
    "fin-nested": "A smaller fin inside a large one",
    "fin-medium-wallet": "2.475 BTC, between small and large",
    "fin-noisy-background": "Descent disturbed by 4 × 1 BTC",
    "fin-double": "One wallet, two fins",
}

CATEGORIES = {
    "liquidity-large-wallet": "liquidity",
    "liquidity-small-wallet": "liquidity",
    "liquidity-two-large-wallets": "liquidity",
    "fin-cliff": "fins",
    "fin-long-tail": "fins",
    "fin-ramp": "fins",
    "fin-spike": "fins",
    "fin-split-whale": "fins",
    "fin-plateau": "fins",
    "fin-sequential": "fins",
    "fin-nested": "fins",
    "fin-medium-wallet": "fins",
    "fin-noisy-background": "fins",
    "fin-double": "fins",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", type=Path, help="overactive-local.json to take the background wallets from")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    base = json.loads(args.base.read_text(encoding="utf-8"))
    if not (VARIANTS.keys() == DESCRIPTIONS.keys() == CATEGORIES.keys()):
        raise RuntimeError("Every generated liquidity scenario needs a description and category")
    for name, extra in VARIANTS.items():
        scenario = {key: value for key, value in base.items() if key != "wallets"}
        scenario.update(
            {
                "name": name,
                "description": DESCRIPTIONS[name],
                "category": CATEGORIES[name],
                "rounds": ROUNDS,
                "wallets": background(base) + extra,
            }
        )
        path = args.output_dir / f"{name}.json"
        path.write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
