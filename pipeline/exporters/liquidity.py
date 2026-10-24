"""Transaction-centric pool liquidity of an emulated run (Svenda et al., PETS 2026, section 4.3.2).

Every coinjoin input and output is classified as entering, remixed, leaving or
staying the mix three times: from the exported blocks and the producer coinjoin
labels (ground truth), from coinjoin-analysis (observed) and from BlockSci over
the CoinJoins it detected. The cumulative liquidity of each classification, its
decomposition by wallet ownership, and how well the observed curve reveals a
large wallet's entry and exit form the report section.
The per-wallet events are exported as the ground-truth dataset.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from pathlib import Path

from exporters.artifact_paths import (
    LIQUIDITY_DATASET,
    LIQUIDITY_EVENTS_CSV,
    POSTMIX_SPENDS_FILE,
)
from exporters.common import JsonObject, compute_optional_rate, load_json, save_json

LIQUIDITY_DATASET_SCHEMA_VERSION = "1.0"
# An output spent within this many later coinjoins leaves or remixes "fast". The paper
# uses one month of mainnet time; an emulated run is ordered by coinjoins instead.
WINDOW_COINJOINS = 3
# A wallet holding at least this share of the pool's interim liquidity at its peak
# mixes more than all other participants together and should be visible.
LARGE_WALLET_SHARE = 0.5

ENTER = "MIX_ENTER"
REMIX = "MIX_REMIX"
LEAVE = "MIX_LEAVE"
STAY = "MIX_STAY"
# Inputs that coinjoin-analysis attributes to other coordinators count as inflow,
# as in its own liquidity computation.
OBSERVED_INFLOW = {ENTER, "MIX_REMIX_FRIENDS", "MIX_REMIX_FRIENDS_WW1"}

CSV_COLUMNS = [
    "coinjoin_index",
    "txid",
    "block_height",
    "side",
    "io_index",
    "wallet",
    "value_sats",
    "event",
    "observed_event",
    "blocksci_event",
    "spent_by",
    "spend_distance_coinjoins",
    "is_standard_denom",
]


def unavailable(reason: str) -> JsonObject:
    return {"available": False, "reason": reason}


def coinjoin_order(transactions: JsonObject, observed: JsonObject) -> list[str]:
    """Coinjoins by confirmation height; broadcast time and txid break ties."""

    def key(txid: str) -> tuple[float, str, str]:
        height = transactions[txid].get("block_height")
        broadcast = (observed.get(txid) or {}).get("broadcast_time") or ""
        return (float("inf") if height is None else float(height), str(broadcast), txid)

    return sorted((txid for txid, record in transactions.items() if record.get("is_coinjoin") is True), key=key)


def spender_index(transactions: JsonObject) -> dict[tuple[str, str], str]:
    spenders: dict[tuple[str, str], str] = {}
    for txid, record in transactions.items():
        for item in record.get("inputs") or []:
            if item.get("prev_txid") is not None and item.get("prev_vout") is not None:
                spenders[(str(item["prev_txid"]), str(item["prev_vout"]))] = txid
    return spenders


def spend_distance(
    created_index: int,
    spender: str,
    positions: dict[str, int],
    transactions: JsonObject,
    coinjoin_heights: list[float],
) -> int | None:
    """Coinjoins confirmed after the output's coinjoin and up to its spender, the spender excluded."""
    if spender in positions:
        return positions[spender] - created_index - 1
    height = (transactions.get(spender) or {}).get("block_height")
    if height is None:
        return None
    return max(bisect_right(coinjoin_heights, float(height)) - created_index - 1, 0)


def standard_denominations(outputs: list[JsonObject]) -> set[int]:
    counts: dict[int, int] = {}
    for item in outputs:
        if isinstance(item.get("value"), int):
            counts[item["value"]] = counts.get(item["value"], 0) + 1
    return {value for value, count in counts.items() if count > 1}


def blocksci_event(mix_events: JsonObject | None, txid: str, side: str, index: object) -> str | None:
    """BlockSci's event for one input or output; its lists are ordered by index."""
    events = ((mix_events or {}).get(txid) or {}).get(side) or []
    try:
        position = int(str(index))
    except ValueError:
        return None
    return events[position] if 0 <= position < len(events) else None


def classify_coinjoins(
    order: list[str],
    transactions: JsonObject,
    observed: JsonObject,
    mix_events: JsonObject | None = None,
) -> list[JsonObject]:
    """Ground-truth, observed and BlockSci event of every coinjoin input and output."""
    positions = {txid: index for index, txid in enumerate(order)}
    heights = [float(transactions[txid].get("block_height") or 0) for txid in order]
    spenders = spender_index(transactions)
    coinjoins = []
    for index, txid in enumerate(order):
        record = transactions[txid]
        observed_record = observed.get(txid) or {}
        observed_inputs = observed_record.get("inputs") or {}
        observed_outputs = observed_record.get("outputs") or {}
        inputs = []
        for item in record.get("inputs") or []:
            inputs.append(
                {
                    "index": item.get("index"),
                    "wallet": item.get("wallet_name"),
                    "value": item.get("value"),
                    "prev_txid": item.get("prev_txid"),
                    "prev_vout": item.get("prev_vout"),
                    "event": REMIX if item.get("prev_txid") in positions else ENTER,
                    "observed_event": (observed_inputs.get(str(item.get("index"))) or {}).get("mix_event_type"),
                    "blocksci_event": blocksci_event(mix_events, txid, "inputs", item.get("index")),
                }
            )
        outputs = []
        standard = standard_denominations(record.get("outputs") or [])
        for item in record.get("outputs") or []:
            spender = spenders.get((txid, str(item.get("index"))))
            if spender is None:
                event, distance = STAY, None
            else:
                event = REMIX if spender in positions else LEAVE
                distance = spend_distance(index, spender, positions, transactions, heights)
            outputs.append(
                {
                    "index": item.get("index"),
                    "wallet": item.get("wallet_name"),
                    "value": item.get("value"),
                    "address": item.get("address"),
                    "event": event,
                    "observed_event": (observed_outputs.get(str(item.get("index"))) or {}).get("mix_event_type"),
                    "blocksci_event": blocksci_event(mix_events, txid, "outputs", item.get("index")),
                    "spent_by": spender,
                    "spend_distance_coinjoins": distance,
                    "is_standard_denom": item.get("value") in standard,
                }
            )
        coinjoins.append(
            {
                "index": index,
                "txid": txid,
                "block_height": record.get("block_height"),
                "inputs": inputs,
                "outputs": outputs,
            }
        )
    return coinjoins


def value_of(item: JsonObject) -> int:
    value = item.get("value")
    return value if isinstance(value, int) else 0


def is_fast(item: JsonObject) -> bool:
    distance = item.get("spend_distance_coinjoins")
    return distance is not None and distance < WINDOW_COINJOINS


def liquidity_series(coinjoins: list[JsonObject], event_field: str, wallet: str | None = None) -> JsonObject:
    """Cumulative liquidity of the pool, or of one wallet's coins, per coinjoin.

    interim: inflow minus outflow, an outflow charged to the coinjoin that created it.
    interim_fast_leave: the same, charging only outputs leaving within the window.
    unmoved_window: outstanding stay or slow-remix outputs, retired when spent.
    active_remix: interim liquidity minus the unmoved outputs.
    """
    enter, leave, stay = [], [], []
    interim, interim_fast_leave, unmoved_window, active_remix = [], [], [], []
    current = current_fast = current_unmoved = 0
    unmoved: dict[tuple[str, str], int] = {}
    inflow_types = OBSERVED_INFLOW if event_field == "observed_event" else {ENTER}
    for coinjoin in coinjoins:
        inputs = [item for item in coinjoin["inputs"] if wallet is None or item.get("wallet") == wallet]
        outputs = [item for item in coinjoin["outputs"] if wallet is None or item.get("wallet") == wallet]
        for item in inputs:
            current_unmoved -= unmoved.pop((str(item.get("prev_txid")), str(item.get("prev_vout"))), 0)
        step_enter = sum(value_of(item) for item in inputs if item.get(event_field) in inflow_types)
        leaving = [item for item in outputs if item.get(event_field) == LEAVE]
        step_leave = sum(value_of(item) for item in leaving)
        step_fast_leave = sum(value_of(item) for item in leaving if is_fast(item))
        # Leaving outputs are already subtracted from interim at creation. A
        # slow remix is inactive only until its outpoint is consumed, not forever.
        for item in outputs:
            if item.get(event_field) in {STAY, REMIX} and not is_fast(item):
                value = value_of(item)
                unmoved[(coinjoin["txid"], str(item["index"]))] = value
                current_unmoved += value
        current += step_enter - step_leave
        current_fast += step_enter - step_fast_leave
        enter.append(step_enter)
        leave.append(step_leave)
        stay.append(sum(value_of(item) for item in outputs if item.get(event_field) == STAY))
        interim.append(current)
        interim_fast_leave.append(current_fast)
        unmoved_window.append(current_unmoved)
        active_remix.append(current - current_unmoved)
    return {
        "enter": enter,
        "leave": leave,
        "stay": stay,
        "interim": interim,
        "interim_fast_leave": interim_fast_leave,
        "unmoved_window": unmoved_window,
        "active_remix": active_remix,
    }


def classification_agreement(coinjoins: list[JsonObject], event_field: str = "observed_event") -> JsonObject:
    """How an analyzer classified each input and output with a ground-truth event."""
    confusion: dict[str, int] = {}
    agreeing = total = 0
    for coinjoin in coinjoins:
        for side in ("inputs", "outputs"):
            for item in coinjoin[side]:
                observed = item.get(event_field) or "missing"
                key = f"{side}:{item['event']}->{observed}"
                confusion[key] = confusion.get(key, 0) + 1
                total += 1
                agreeing += observed == item["event"]
    return {
        "classified": total,
        "agreeing": agreeing,
        "agreement_rate": compute_optional_rate(agreeing, total),
        "confusion": dict(sorted(confusion.items())),
    }


def first_index(values: list[int]) -> int | None:
    return next((index for index, value in enumerate(values) if value > 0), None)


def last_index(values: list[int]) -> int | None:
    return next((index for index in range(len(values) - 1, -1, -1) if values[index] > 0), None)


def wallet_summaries(coinjoins: list[JsonObject], pool: JsonObject) -> dict[str, JsonObject]:
    names = sorted(
        {
            item["wallet"]
            for coinjoin in coinjoins
            for side in ("inputs", "outputs")
            for item in coinjoin[side]
            if item.get("wallet")
        }
    )
    summaries = {}
    for name in names:
        series = liquidity_series(coinjoins, "event", name)
        peak = max(series["interim"], default=0)
        peak_index = series["interim"].index(peak) if series["interim"] else None
        pool_at_peak = pool["interim"][peak_index] if peak_index is not None else 0
        participated = sum(
            1 for coinjoin in coinjoins if any(item.get("wallet") == name for item in coinjoin["inputs"])
        )
        summaries[name] = {
            "coinjoins_participated": participated,
            "entered_sats": sum(series["enter"]),
            "left_sats": sum(series["leave"]),
            "stayed_sats": sum(series["stay"]),
            "first_enter_index": first_index(series["enter"]),
            "last_enter_index": last_index(series["enter"]),
            "first_leave_index": first_index(series["leave"]),
            "last_leave_index": last_index(series["leave"]),
            "peak_interim_sats": peak,
            "peak_index": peak_index,
            "peak_pool_share": round(peak / pool_at_peak, 6) if pool_at_peak > 0 else None,
            "series": series,
        }
    return summaries


def descending_window(interim: list[int]) -> tuple[int, int] | None:
    """Coinjoins from the observed peak down to the lowest point after it."""
    if not interim:
        return None
    peak = interim.index(max(interim))
    tail = interim[peak:]
    trough = peak + tail.index(min(tail))
    return peak, trough


def fin_evaluation(coinjoins: list[JsonObject], observed: JsonObject, wallet: str) -> JsonObject:
    """How the observed curve reveals the wallet's entry and its postmix spends.

    The paper attributes outputs leaving during the descending part of a "fin" to the
    large wallet; precision is the wallet's share of the value leaving in that window,
    lift compares it to the wallet's share of all value leaving the mix.
    """
    entry_index = observed["enter"].index(max(observed["enter"])) if observed["enter"] else None
    entry_inputs = coinjoins[entry_index]["inputs"] if entry_index is not None else []
    entry_total = sum(value_of(item) for item in entry_inputs if item["event"] == ENTER)
    entry_wallet = sum(
        value_of(item) for item in entry_inputs if item["event"] == ENTER and item.get("wallet") == wallet
    )

    window = descending_window(observed["interim"])
    leaving = [
        (coinjoin["index"], item) for coinjoin in coinjoins for item in coinjoin["outputs"] if item["event"] == LEAVE
    ]
    all_left = sum(value_of(item) for _, item in leaving)
    wallet_left = sum(value_of(item) for _, item in leaving if item.get("wallet") == wallet)
    in_window = [item for index, item in leaving if window is not None and window[0] <= index <= window[1]]
    window_left = sum(value_of(item) for item in in_window)
    window_wallet_left = sum(value_of(item) for item in in_window if item.get("wallet") == wallet)
    precision = round(window_wallet_left / window_left, 6) if window_left else None
    baseline = round(wallet_left / all_left, 6) if all_left else None
    return {
        "observed_entry_index": entry_index,
        "entry_wallet_share": round(entry_wallet / entry_total, 6) if entry_total else None,
        "descending_window": list(window) if window is not None else None,
        "window_leave_sats": window_left,
        "window_leave_precision": precision,
        "window_leave_recall": round(window_wallet_left / wallet_left, 6) if wallet_left else None,
        "baseline_leave_share": baseline,
        "lift": round(precision / baseline, 6) if precision is not None and baseline else None,
    }


def load_postmix_spends(run_dir: Path, transactions: JsonObject, positions: dict[str, int]) -> JsonObject | None:
    """Spends the emulator scheduled, checked against the exported blocks."""
    path = run_dir / POSTMIX_SPENDS_FILE
    if not path.is_file():
        return None
    spends = []
    for spend in load_json(path).get("spends") or []:
        txid = spend.get("txid")
        inputs = (transactions.get(txid) or {}).get("inputs") or [] if txid else []
        spends.append(
            {
                "wallet": f"wallet-{int(spend['wallet_index']):03}" if spend.get("wallet_index") is not None else None,
                "status": spend.get("status"),
                "txid": txid,
                "amount_sats": spend.get("amount_sats"),
                "round": spend.get("round"),
                "block": spend.get("block"),
                "delay_rounds": spend.get("delay_rounds"),
                "delay_blocks": spend.get("delay_blocks"),
                "confirmed": bool(txid and txid in transactions),
                "spends_coinjoin_outputs": any(item.get("prev_txid") in positions for item in inputs),
            }
        )
    statuses: dict[str, int] = {}
    for spend in spends:
        statuses[str(spend["status"])] = statuses.get(str(spend["status"]), 0) + 1
    return {
        "scheduled": len(spends),
        "by_status": dict(sorted(statuses.items())),
        "confirmed_postmix": sum(1 for spend in spends if spend["confirmed"] and spend["spends_coinjoin_outputs"]),
        "spends": spends,
    }


def series_totals(series: JsonObject) -> JsonObject:
    return {
        "entered_sats": sum(series["enter"]),
        "left_sats": sum(series["leave"]),
        "stayed_sats": sum(series["stay"]),
        "final_interim_sats": series["interim"][-1],
        "peak_interim_sats": max(series["interim"]),
    }


def max_interim_difference(pool: JsonObject, series: JsonObject) -> int:
    return max((abs(a - b) for a, b in zip(pool["interim"], series["interim"])), default=0)


def blocksci_liquidity(
    coinjoins: list[JsonObject],
    pool: JsonObject,
    mix_events: JsonObject | None,
    error: str | None,
) -> tuple[JsonObject, JsonObject | None]:
    """BlockSci's classification against the ground truth, and its pool series.

    BlockSci's mix is the CoinJoins it detected, so a missed CoinJoin leaves its
    inputs and outputs unclassified and a false positive turns inputs it funds
    into remixes.
    """
    if mix_events is None:
        return unavailable(error or "BlockSci did not classify liquidity events."), None
    series = liquidity_series(coinjoins, "blocksci_event")
    labelled = {coinjoin["txid"] for coinjoin in coinjoins}
    return {
        "available": True,
        **series_totals(series),
        "unclassified_coinjoins": len(labelled - set(mix_events)),
        "extra_coinjoins": len(set(mix_events) - labelled),
        "interim_max_abs_difference_sats": max_interim_difference(pool, series),
        "classification_agreement": classification_agreement(coinjoins, "blocksci_event"),
    }, series


def build_liquidity(
    run_dir: Path,
    emulator_data: JsonObject | None,
    coinjoin_analysis_data: JsonObject,
    blocksci_mix_events: JsonObject | None = None,
    blocksci_error: str | None = None,
) -> tuple[JsonObject, JsonObject | None]:
    """Return the report section and the dataset, or an unavailable section and None."""
    if not emulator_data:
        return unavailable("Liquidity ground truth needs emulator data; this is not an emulator report."), None
    if not (emulator_data.get("label_provenance") or {}).get("independent"):
        return unavailable("Emulator coinjoin labels are unavailable, so the mix boundary is unknown."), None
    transactions = emulator_data.get("transactions") or {}
    observed = coinjoin_analysis_data.get("coinjoins") or {}
    order = coinjoin_order(transactions, observed)
    if not order:
        return unavailable("The emulator exported no labelled coinjoin."), None

    coinjoins = classify_coinjoins(order, transactions, observed, blocksci_mix_events)
    pool = liquidity_series(coinjoins, "event")
    observed_pool = liquidity_series(coinjoins, "observed_event")
    blocksci_section, blocksci_pool = blocksci_liquidity(coinjoins, pool, blocksci_mix_events, blocksci_error)
    wallets = wallet_summaries(coinjoins, pool)
    dominant = max(wallets, key=lambda name: wallets[name]["peak_interim_sats"], default=None)
    large = [name for name, summary in wallets.items() if (summary["peak_pool_share"] or 0) >= LARGE_WALLET_SHARE]
    postmix_spends = load_postmix_spends(run_dir, transactions, {txid: index for index, txid in enumerate(order)})

    section = {
        "available": True,
        "parameters": {"window_coinjoins": WINDOW_COINJOINS, "large_wallet_share": LARGE_WALLET_SHARE},
        "coinjoins": len(coinjoins),
        "ground_truth": series_totals(pool),
        "observed": series_totals(observed_pool),
        "interim_max_abs_difference_sats": max_interim_difference(pool, observed_pool),
        "classification_agreement": classification_agreement(coinjoins),
        "blocksci": blocksci_section,
        "dominant_wallet": dominant,
        "large_wallets": [
            {
                "wallet": name,
                **{key: value for key, value in wallets[name].items() if key != "series"},
                "fin": fin_evaluation(coinjoins, observed_pool, name),
            }
            for name in large
        ],
        "postmix_spends": (
            {key: value for key, value in postmix_spends.items() if key != "spends"} if postmix_spends else None
        ),
        "dataset_files": [LIQUIDITY_DATASET, LIQUIDITY_EVENTS_CSV],
    }
    dataset = {
        "schema_version": LIQUIDITY_DATASET_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "coinjoin_type": emulator_data.get("coinjoin_type"),
        "parameters": section["parameters"],
        "pool": {"ground_truth": pool, "observed": observed_pool, "blocksci": blocksci_pool},
        "wallets": wallets,
        "coinjoins": coinjoins,
        "postmix_spends": postmix_spends["spends"] if postmix_spends else [],
    }
    return section, dataset


def save_liquidity_dataset(output_dir: Path, dataset: JsonObject) -> None:
    """Write the dataset as JSON and its coinjoin events as one CSV row each."""
    save_json(output_dir / LIQUIDITY_DATASET, dataset)
    with (output_dir / LIQUIDITY_EVENTS_CSV).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for coinjoin in dataset["coinjoins"]:
            for side in ("inputs", "outputs"):
                for item in coinjoin[side]:
                    writer.writerow(
                        {
                            "coinjoin_index": coinjoin["index"],
                            "txid": coinjoin["txid"],
                            "block_height": coinjoin["block_height"],
                            "side": side[:-1],
                            "io_index": item.get("index"),
                            "wallet": item.get("wallet"),
                            "value_sats": item.get("value"),
                            "event": item["event"],
                            "observed_event": item.get("observed_event"),
                            "blocksci_event": item.get("blocksci_event"),
                            "spent_by": item.get("spent_by"),
                            "spend_distance_coinjoins": item.get("spend_distance_coinjoins"),
                            "is_standard_denom": item.get("is_standard_denom"),
                        }
                    )
