"""Python explanations for BlockSci CoinJoin detector rules."""

from __future__ import annotations

from exporters.common import (
    DEFAULT_FIRST_WASABI2_BLOCK,
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    WASABI2_ALLOWED_ADDRESS_TYPES,
    WASABI2_MAX_SATOSHIS,
    WASABI2_MIN_SATOSHIS,
    WASABI2_THRESHOLD_CHANGE_BLOCK,
    JsonObject,
    coerce_int,
    rule_result,
)


def values_descending(records: list[JsonObject]) -> bool:
    values: list[int] = []
    for record in records:
        value = record.get("value")
        if value is None:
            return False
        values.append(int(value))
    return all(values[index] >= values[index + 1] for index in range(len(values) - 1))


def unique_addresses(records: list[JsonObject]) -> set[str]:
    return {str(record["address"]) for record in records if record.get("address")}


def address_type_rule(name: str, records: list[JsonObject]) -> JsonObject:
    address_types = [str(record["address_type"]) for record in records if record.get("address_type")]
    expected = "WITNESS_PUBKEYHASH or WITNESS_UNKNOWN"
    if not address_types:
        return rule_result(name, None, "not available", expected)
    script_types_by_address_type: dict[str, set[str]] = {}
    for record in records:
        address_type = record.get("address_type")
        script_type = record.get("script_type")
        if address_type and script_type:
            script_types_by_address_type.setdefault(str(address_type), set()).add(str(script_type))
    invalid = sorted(
        {address_type for address_type in address_types if address_type not in WASABI2_ALLOWED_ADDRESS_TYPES}
    )
    observed = []
    for address_type in sorted(set(address_types)):
        script_types = script_types_by_address_type.get(str(address_type), set())
        if address_type == "WITNESS_UNKNOWN" and "witness_v1_taproot" in script_types:
            observed.append("WITNESS_UNKNOWN (taproot/witness_v1_taproot)")
        else:
            observed.append(str(address_type))
    return rule_result(name, not invalid, ", ".join(observed), expected)


def wasabi2_default_input_threshold(block_height: int | None, test_values: bool) -> int:
    if block_height is not None and block_height >= WASABI2_THRESHOLD_CHANGE_BLOCK:
        return 20
    return 20 if test_values else 50


def wasabi2_blocksci_denominations() -> set[int]:
    denominations = {WASABI2_MIN_SATOSHIS, WASABI2_MAX_SATOSHIS}

    denom = 1
    while True:
        denom *= 2
        if denom >= WASABI2_MIN_SATOSHIS:
            denominations.add(denom)
        if denom > WASABI2_MAX_SATOSHIS:
            break

    denom = 3
    while True:
        denom *= 3
        if denom >= WASABI2_MIN_SATOSHIS:
            denominations.add(denom)
        if denom > WASABI2_MAX_SATOSHIS:
            break

    denom = 3
    while True:
        denom *= 3
        next_denom = denom * 2
        if denom >= WASABI2_MIN_SATOSHIS:
            denominations.add(next_denom)
        if denom > WASABI2_MAX_SATOSHIS:
            break

    denom = 10
    while True:
        denom *= 10
        if denom >= WASABI2_MIN_SATOSHIS:
            denominations.add(denom)
        if denom > WASABI2_MAX_SATOSHIS:
            break

    denom = 10
    while True:
        denom *= 10
        next_denom = denom * 5
        if denom >= WASABI2_MIN_SATOSHIS:
            denominations.add(next_denom)
        if denom > WASABI2_MAX_SATOSHIS:
            break

    denom = 10
    while True:
        denom *= 10
        next_denom = denom * 2
        if denom >= WASABI2_MIN_SATOSHIS:
            denominations.add(next_denom)
        if denom > WASABI2_MAX_SATOSHIS:
            break

    return denominations


WASABI2_BLOCKSCI_DENOMINATIONS = wasabi2_blocksci_denominations()


def wasabi2_known_denomination_count(outputs: list[JsonObject]) -> int:
    return sum(1 for output in outputs if output.get("value") in WASABI2_BLOCKSCI_DENOMINATIONS)


def wasabi2_denomination_ratio(block_height: int | None) -> float:
    if block_height is not None and block_height >= WASABI2_THRESHOLD_CHANGE_BLOCK:
        return 0.75
    return 0.8


def explain_wasabi2_heuristic(
    record: JsonObject,
    min_input_count: int | None = None,
    test_values: bool = False,
    first_wasabi2_block: int = DEFAULT_FIRST_WASABI2_BLOCK,
) -> JsonObject:
    inputs = record.get("inputs", [])
    outputs = record.get("outputs", [])
    block_height = coerce_int(record.get("block_height"))
    input_threshold = (
        min_input_count
        if min_input_count is not None
        else wasabi2_default_input_threshold(block_height, test_values)
    )
    input_threshold_source = "--min-input-count" if min_input_count is not None else "BlockSci default"
    input_addresses = unique_addresses(inputs)
    output_addresses = unique_addresses(outputs)
    denom_count = wasabi2_known_denomination_count(outputs)
    denom_ratio = wasabi2_denomination_ratio(block_height)
    denom_required = len(outputs) * denom_ratio

    rules = [
        rule_result(
            "first_wasabi2_block",
            block_height is not None and int(block_height) >= first_wasabi2_block,
            block_height if block_height is not None else "missing",
            f">= {first_wasabi2_block}",
        ),
        address_type_rule("input_address_types", inputs),
        address_type_rule("output_address_types", outputs),
        rule_result(
            "input_count",
            len(inputs) >= input_threshold,
            len(inputs),
            f">= {input_threshold} ({input_threshold_source})",
        ),
        rule_result(
            "input_values_descending",
            values_descending(inputs),
            [item.get("value") for item in inputs],
            "non-increasing input values",
        ),
        rule_result("unique_input_addresses", len(input_addresses) >= 5, len(input_addresses), ">= 5"),
        rule_result(
            "output_values_descending",
            values_descending(outputs),
            [item.get("value") for item in outputs],
            "non-increasing output values",
        ),
        rule_result("unique_output_addresses", len(output_addresses) >= 5, len(output_addresses), ">= 5"),
        rule_result(
            "wasabi2_denominations",
            denom_count > denom_required,
            f"{denom_count}/{len(outputs)} outputs",
            f"> {denom_required:.2f} outputs ({int(denom_ratio * 100)}% threshold)",
        ),
    ]
    failed_rules = [rule["name"] for rule in rules if rule["passed"] is False]
    return {
        "heuristic": "wasabi2",
        "would_pass_python_rules": not failed_rules,
        "failed_rules": failed_rules,
        "rules": rules,
    }


def grouped_input_values_by_address(inputs: list[JsonObject]) -> dict[str, int]:
    grouped: dict[str, int] = {}
    for input_record in inputs:
        address = input_record.get("address")
        value = input_record.get("value")
        if address is None or value is None:
            continue
        grouped[str(address)] = grouped.get(str(address), 0) + int(value)
    return grouped


def output_addresses_by_value(outputs: list[JsonObject]) -> dict[int, set[str]]:
    grouped: dict[int, set[str]] = {}
    for output_record in outputs:
        value = output_record.get("value")
        address = output_record.get("address")
        if value is None or address is None:
            continue
        grouped.setdefault(int(value), set()).add(str(address))
    return grouped


def output_value_counts(outputs: list[JsonObject]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for output_record in outputs:
        value = output_record.get("value")
        if value is None:
            continue
        counts[int(value)] = counts.get(int(value), 0) + 1
    return counts


def format_int_list(values: list[int]) -> str:
    return ", ".join(str(value) for value in values) if values else "-"


def joinmarket_subset_result(
    values: list[int],
    bucket_goals: list[int],
    max_depth: int,
) -> tuple[bool | None, str, int]:
    values = sorted(values)
    goals = sorted(bucket_goals, reverse=True)
    depth = 0

    def remaining(goal: int, current: int) -> int:
        return goal - current if goal > current else 0

    def search(
        remaining_values: list[int],
        buckets: list[tuple[int, int]],
        total_remaining: int,
        value_left: int,
    ) -> tuple[bool | None, str]:
        nonlocal depth
        if total_remaining > value_left:
            return False, "total bucket target exceeds remaining input value"

        active_buckets = [(goal, current) for goal, current in buckets if current < goal]
        if not active_buckets:
            return True, "all buckets filled"

        if not remaining_values:
            return False, "inputs exhausted before buckets filled"

        depth += 1
        if max_depth != 0 and depth > max_depth:
            return None, "max-depth timeout"

        active_buckets.sort(key=lambda bucket: remaining(bucket[0], bucket[1]), reverse=True)
        value = remaining_values[-1]
        next_values = remaining_values[:-1]
        next_value_left = value_left - value
        for index, (goal, current) in enumerate(active_buckets):
            old_remaining = remaining(goal, current)
            next_buckets = list(active_buckets)
            next_buckets[index] = (goal, current + value)
            new_remaining = remaining(goal, current + value)
            next_total_remaining = total_remaining - old_remaining + new_remaining
            result, reason = search(
                next_values,
                next_buckets,
                next_total_remaining,
                next_value_left,
            )
            if result is not False:
                return result, reason

        return False, "no partition of grouped input values can fill bucket goals"

    result, reason = search(values, [(goal, 0) for goal in goals], sum(goals), sum(values))
    return result, reason, depth


def explain_joinmarket_definite_heuristic(
    record: JsonObject,
    min_base_fee: int = DEFAULT_JOINMARKET_MIN_BASE_FEE,
    percentage_fee: float = DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    max_depth: int = DEFAULT_JOINMARKET_MAX_DEPTH,
) -> JsonObject:
    inputs = record.get("inputs", [])
    outputs = record.get("outputs", [])
    input_values_by_address = grouped_input_values_by_address(inputs)
    output_addresses = output_addresses_by_value(outputs)
    value_counts = output_value_counts(outputs)
    participant_count = (len(outputs) + 1) // 2
    dominant_value = None
    dominant_address_count = 0
    if output_addresses:
        dominant_value, dominant_addresses = max(output_addresses.items(), key=lambda item: len(item[1]))
        dominant_address_count = len(dominant_addresses)

    max_possible_fee = None
    bucket_goals: list[int] = []
    fee_adjusted_bucket_goals: list[int] = []
    subset_passed: bool | None = None
    subset_reason = "not evaluated"
    subset_depth = 0

    if dominant_value is not None:
        max_possible_fee = max(min_base_fee, int(dominant_value * percentage_fee))
        bucket_goals = [dominant_value] * participant_count
        bucket_index = 0
        for output in outputs:
            value = output.get("value")
            if value is None or int(value) == dominant_value:
                continue
            if bucket_index < len(bucket_goals):
                bucket_goals[bucket_index] += int(value)
            bucket_index += 1
        fee_adjusted_bucket_goals = [
            0 if max_possible_fee > goal else goal - max_possible_fee
            for goal in bucket_goals
        ]
        subset_passed, subset_reason, subset_depth = joinmarket_subset_result(
            list(input_values_by_address.values()),
            fee_adjusted_bucket_goals,
            max_depth,
        )

    repeated_other_values = {
        value: count
        for value, count in sorted(value_counts.items())
        if count > 1 and value != dominant_value
    }
    subset_observed = {
        "input_values": sorted(input_values_by_address.values(), reverse=True),
        "bucket_goals_after_fee": fee_adjusted_bucket_goals,
        "search_depth": subset_depth,
        "reason": subset_reason,
    }

    rules = [
        rule_result(
            "basic_size",
            len(inputs) >= 2 and len(outputs) >= 3,
            f"{len(inputs)} inputs / {len(outputs)} outputs",
            ">= 2 inputs and >= 3 outputs",
        ),
        rule_result(
            "participant_count_vs_inputs",
            participant_count <= len(inputs),
            participant_count,
            f"<= input count ({len(inputs)})",
        ),
        rule_result(
            "participant_count_vs_unique_input_addresses",
            participant_count <= len(input_values_by_address),
            len(input_values_by_address),
            f">= participant count ({participant_count})",
        ),
        rule_result(
            "dominant_output_address_count",
            dominant_address_count == participant_count,
            f"{dominant_value} x {dominant_address_count}" if dominant_value is not None else "missing",
            f"one output value with {participant_count} distinct addresses",
        ),
        rule_result(
            "dominant_output_value_not_dust_exclusion",
            dominant_value not in (546, 2730) if dominant_value is not None else False,
            dominant_value if dominant_value is not None else "missing",
            "not 546 or 2730 sats",
        ),
        rule_result(
            "subset_partition_after_fee",
            subset_passed,
            subset_observed,
            (
                "grouped input address values can fill participant buckets "
                f"after max({min_base_fee}, dominant_value * {percentage_fee}) fee"
            ),
        ),
    ]
    failed_rules = [rule["name"] for rule in rules if rule["passed"] is False]
    return {
        "heuristic": "joinmarket_definite",
        "would_pass_python_rules": not failed_rules and subset_passed is True,
        "failed_rules": failed_rules,
        "rules": rules,
        "parameters": {
            "min_base_fee": min_base_fee,
            "percentage_fee": percentage_fee,
            "max_depth": max_depth,
            "max_possible_fee": max_possible_fee,
            "participant_count": participant_count,
            "dominant_output_value": dominant_value,
            "repeated_non_mix_output_values": repeated_other_values,
        },
    }


def explain_joinmarket_possible_heuristic(
    record: JsonObject,
    min_base_fee: int = DEFAULT_JOINMARKET_MIN_BASE_FEE,
    percentage_fee: float = DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    max_depth: int = DEFAULT_JOINMARKET_MAX_DEPTH,
) -> JsonObject:
    inputs = record.get("inputs", [])
    outputs = record.get("outputs", [])
    input_values_by_address = grouped_input_values_by_address(inputs)
    input_addresses = set(input_values_by_address)
    unknown_outputs = [
        output
        for output in outputs
        if output.get("address") is not None and str(output.get("address")) not in input_addresses
    ]
    all_output_counts = output_value_counts(outputs)
    unknown_output_counts = output_value_counts(unknown_outputs)
    dominant_value = None
    dominant_count = 0
    if all_output_counts:
        dominant_value, dominant_count = max(all_output_counts.items(), key=lambda item: item[1])

    unknown_dominant_value = None
    unknown_dominant_count = 0
    if unknown_output_counts:
        unknown_dominant_value, unknown_dominant_count = max(unknown_output_counts.items(), key=lambda item: item[1])

    goal_value = None
    max_possible_fee = None
    bucket_goals: list[int] = []
    subset_passed: bool | None = None
    subset_reason = "not evaluated"
    subset_depth = 0
    if unknown_dominant_value is not None:
        max_possible_fee = max(min_base_fee, int(unknown_dominant_value * percentage_fee))
        goal_value = max(0, unknown_dominant_value - max_possible_fee)
        bucket_goals = [goal_value, goal_value]
        subset_passed, subset_reason, subset_depth = joinmarket_subset_result(
            list(input_values_by_address.values()),
            bucket_goals,
            max_depth,
        )

    rules = [
        rule_result(
            "not_single_input_or_output",
            len(inputs) != 1 and len(outputs) != 1,
            f"{len(inputs)} inputs / {len(outputs)} outputs",
            "input count != 1 and output count != 1",
        ),
        rule_result(
            "has_repeated_output_value",
            dominant_count > 1,
            f"{dominant_value} x {dominant_count}" if dominant_value is not None else "missing",
            "at least two outputs with the same value",
        ),
        rule_result(
            "multiple_input_addresses",
            len(input_values_by_address) != 1,
            len(input_values_by_address),
            "not exactly one grouped input address",
        ),
        rule_result(
            "multiple_unknown_outputs",
            len(unknown_outputs) > 1,
            len(unknown_outputs),
            "> 1 output address not also present in inputs",
        ),
        rule_result(
            "has_repeated_unknown_output_value",
            unknown_dominant_count > 1,
            (
                f"{unknown_dominant_value} x {unknown_dominant_count}"
                if unknown_dominant_value is not None
                else "missing"
            ),
            "at least two unknown outputs with the same value",
        ),
        rule_result(
            "two_bucket_subset_after_fee",
            subset_passed,
            {
                "input_values": sorted(input_values_by_address.values(), reverse=True),
                "bucket_goals_after_fee": bucket_goals,
                "search_depth": subset_depth,
                "reason": subset_reason,
            },
            (
                "grouped input address values can fill two buckets after "
                f"max({min_base_fee}, dominant_unknown_value * {percentage_fee}) fee"
            ),
        ),
    ]
    failed_rules = [rule["name"] for rule in rules if rule["passed"] is False]
    return {
        "heuristic": "joinmarket_possible",
        "would_pass_python_rules": not failed_rules and subset_passed is True,
        "failed_rules": failed_rules,
        "rules": rules,
        "parameters": {
            "min_base_fee": min_base_fee,
            "percentage_fee": percentage_fee,
            "max_depth": max_depth,
            "max_possible_fee": max_possible_fee,
            "dominant_output_value": dominant_value,
            "dominant_unknown_output_value": unknown_dominant_value,
            "goal_value": goal_value,
        },
    }


def add_blocksci_heuristic_explanations(
    records: dict[str, JsonObject],
    coinjoin_type: str,
    min_input_count: int | None = None,
    test_values: bool = False,
    first_wasabi2_block: int = DEFAULT_FIRST_WASABI2_BLOCK,
    joinmarket_detector: str = DEFAULT_JOINMARKET_DETECTOR,
    joinmarket_min_base_fee: int = DEFAULT_JOINMARKET_MIN_BASE_FEE,
    joinmarket_percentage_fee: float = DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    joinmarket_max_depth: int = DEFAULT_JOINMARKET_MAX_DEPTH,
) -> None:
    if coinjoin_type == "wasabi2":
        for record in records.values():
            record["blocksci_heuristic_explanation"] = explain_wasabi2_heuristic(
                record,
                min_input_count=min_input_count,
                test_values=test_values,
                first_wasabi2_block=first_wasabi2_block,
            )
    elif coinjoin_type == "joinmarket" and joinmarket_detector == "definite":
        for record in records.values():
            record["blocksci_heuristic_explanation"] = explain_joinmarket_definite_heuristic(
                record,
                min_base_fee=joinmarket_min_base_fee,
                percentage_fee=joinmarket_percentage_fee,
                max_depth=joinmarket_max_depth,
            )
    elif coinjoin_type == "joinmarket" and joinmarket_detector == "possible":
        for record in records.values():
            record["blocksci_heuristic_explanation"] = explain_joinmarket_possible_heuristic(
                record,
                min_base_fee=joinmarket_min_base_fee,
                percentage_fee=joinmarket_percentage_fee,
                max_depth=joinmarket_max_depth,
            )
