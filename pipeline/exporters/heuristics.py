"""Python explanations for BlockSci CoinJoin detector rules."""

from __future__ import annotations

from exporters.common import (
    DEFAULT_FIRST_WASABI2_BLOCK,
    WASABI2_ALLOWED_ADDRESS_TYPES,
    WASABI2_MAX_SATOSHIS,
    WASABI2_MIN_SATOSHIS,
    WASABI2_THRESHOLD_CHANGE_BLOCK,
    JsonObject,
    coerce_int,
    rule_result,
)
from exporters.report_types import IORecord, TransactionRecord
from exporters.parameters import Detector


def values_descending(records: list[IORecord]) -> bool:
    values: list[int] = []
    for record in records:
        value = record.get("value")
        if value is None:
            return False
        values.append(int(value))
    return all(values[index] >= values[index + 1] for index in range(len(values) - 1))


def unique_addresses(records: list[IORecord]) -> set[str]:
    return {str(record["address"]) for record in records if record.get("address")}


def address_type_rule(name: str, records: list[IORecord]) -> JsonObject:
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


def wasabi2_default_input_threshold(block_height: int | None) -> int:
    if block_height is not None and block_height >= WASABI2_THRESHOLD_CHANGE_BLOCK:
        return 20
    return 50


def wasabi2_blocksci_denominations() -> set[int]:
    """Mirror of BlockSci's CoinjoinUtils::compute_ww2_denominations.

    Each series only contributes values inside [min, max]; the bound check
    happens before insertion, exactly like the C++ implementation.
    """
    denominations = {WASABI2_MIN_SATOSHIS, WASABI2_MAX_SATOSHIS}

    def add_series(base: int, factor: int, multiplier: int) -> None:
        value = base
        while True:
            value *= factor
            candidate = value * multiplier
            if candidate < WASABI2_MIN_SATOSHIS:
                continue
            if candidate > WASABI2_MAX_SATOSHIS:
                break
            denominations.add(candidate)

    add_series(1, 2, 1)  # powers of 2
    add_series(3, 3, 1)  # powers of 3
    add_series(3, 3, 2)  # powers of 3, times 2
    add_series(10, 10, 1)  # powers of 10 (1-2-5 series)
    add_series(10, 10, 2)
    add_series(10, 10, 5)

    return denominations


WASABI2_BLOCKSCI_DENOMINATIONS = wasabi2_blocksci_denominations()


def wasabi2_known_denomination_count(outputs: list[IORecord]) -> int:
    return sum(1 for output in outputs if output.get("value") in WASABI2_BLOCKSCI_DENOMINATIONS)


def wasabi2_denomination_ratio(block_height: int | None) -> float:
    if block_height is not None and block_height >= WASABI2_THRESHOLD_CHANGE_BLOCK:
        return 0.75
    return 0.8


def explain_wasabi2_heuristic(
    record: TransactionRecord,
    min_input_count: int | None = None,
    first_wasabi2_block: int = DEFAULT_FIRST_WASABI2_BLOCK,
) -> JsonObject:
    inputs = record.get("inputs", [])
    outputs = record.get("outputs", [])
    block_height = coerce_int(record.get("block_height"))
    input_threshold = min_input_count if min_input_count is not None else wasabi2_default_input_threshold(block_height)
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


# Mirror of BlockSci's isJoinMarketCoinJoin (src/heuristics/tx_identification.cpp).
JOINMARKET_MIN_MIX_OUTPUTS = 5
JOINMARKET_MAX_INPUTS_PER_MIX_OUTPUT = 5
JOINMARKET_MIN_MIX_VALUE = 10_000
JOINMARKET_MIN_CHANGE_VALUE = 5_000
JOINMARKET_MIN_SEGWIT_INPUT_FRACTION = 0.3
JOINMARKET_REJECTED_OUTPUT_SCRIPT_TYPES = frozenset({"nulldata", "nonstandard"})


def output_value_counts(outputs: list[IORecord]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for output_record in outputs:
        value = output_record.get("value")
        if value is None:
            continue
        counts[int(value)] = counts.get(int(value), 0) + 1
    return counts


def explain_joinmarket_heuristic(record: TransactionRecord) -> JsonObject:
    inputs = record.get("inputs", [])
    outputs = record.get("outputs", [])
    value_counts = output_value_counts(outputs)
    mix_count = max(value_counts.values(), default=0)
    mix_values = [value for value, count in value_counts.items() if count == mix_count]
    mix_value = mix_values[0] if len(mix_values) == 1 else None
    other_repeated_values = sorted(value for value, count in value_counts.items() if 1 < count < mix_count)
    output_script_types = [str(output["script_type"]) for output in outputs if output.get("script_type")]
    rejected_script_types = sorted(set(output_script_types) & JOINMARKET_REJECTED_OUTPUT_SCRIPT_TYPES)
    address_uses: dict[str, int] = {}
    for io_record in [*inputs, *outputs]:
        if io_record.get("address"):
            address_uses[str(io_record["address"])] = address_uses.get(str(io_record["address"]), 0) + 1
    reused_addresses = sorted(address for address, uses in address_uses.items() if uses > 1)
    change_values = [
        int(value) for output in outputs if (value := output.get("value")) is not None and value != mix_value
    ]
    input_script_types = [str(item["script_type"]) for item in inputs if item.get("script_type")]
    segwit_fraction = (
        sum(1 for script_type in input_script_types if script_type.startswith("witness_")) / len(inputs)
        if inputs and len(input_script_types) == len(inputs)
        else None
    )

    rules = [
        rule_result(
            "no_op_return_or_nonstandard_outputs",
            not rejected_script_types if output_script_types else None,
            ", ".join(rejected_script_types) or ("none" if output_script_types else "not available"),
            "no nulldata or nonstandard output",
        ),
        rule_result(
            "mix_output_count",
            mix_count >= JOINMARKET_MIN_MIX_OUTPUTS,
            mix_count,
            f">= {JOINMARKET_MIN_MIX_OUTPUTS} outputs of the most common value",
        ),
        rule_result(
            "single_repeated_value",
            mix_value is not None and not other_repeated_values,
            {"mix_values": sorted(mix_values), "other_repeated_values": other_repeated_values},
            "exactly one output value repeats",
        ),
        rule_result(
            "basic_size",
            len(inputs) >= 2 and len(outputs) >= 3,
            f"{len(inputs)} inputs / {len(outputs)} outputs",
            ">= 2 inputs and >= 3 outputs",
        ),
        rule_result(
            "inputs_per_mix_output",
            mix_count <= len(inputs) <= JOINMARKET_MAX_INPUTS_PER_MIX_OUTPUT * mix_count,
            len(inputs),
            f"{mix_count} to {JOINMARKET_MAX_INPUTS_PER_MIX_OUTPUT * mix_count} inputs",
        ),
        rule_result("output_count", len(outputs) <= 2 * mix_count + 1, len(outputs), f"<= {2 * mix_count + 1}"),
        rule_result(
            "no_address_reuse",
            not reused_addresses,
            reused_addresses or "none",
            "every input and output address appears once",
        ),
        rule_result(
            "mix_value",
            mix_value is not None and mix_value >= JOINMARKET_MIN_MIX_VALUE,
            mix_value if mix_value is not None else "missing",
            f">= {JOINMARKET_MIN_MIX_VALUE} sats",
        ),
        rule_result(
            "change_values",
            all(value >= JOINMARKET_MIN_CHANGE_VALUE for value in change_values),
            min(change_values, default="none"),
            f"every change output >= {JOINMARKET_MIN_CHANGE_VALUE} sats",
        ),
        rule_result("change_output_count", len(change_values) <= mix_count, len(change_values), f"<= {mix_count}"),
        rule_result(
            "segwit_inputs",
            segwit_fraction >= JOINMARKET_MIN_SEGWIT_INPUT_FRACTION if segwit_fraction is not None else None,
            round(segwit_fraction, 3) if segwit_fraction is not None else "not available",
            f">= {JOINMARKET_MIN_SEGWIT_INPUT_FRACTION} of inputs spend segwit outputs",
        ),
    ]
    failed_rules = [rule["name"] for rule in rules if rule["passed"] is False]
    return {
        "heuristic": "joinmarket",
        "would_pass_python_rules": not failed_rules,
        "failed_rules": failed_rules,
        "rules": rules,
    }


def add_blocksci_heuristic_explanations(records: dict[str, TransactionRecord], detector: Detector) -> None:
    """Explain, rule by rule, why the selected BlockSci heuristic accepts or rejects each record."""
    if detector.coinjoin_type == "wasabi2":
        for record in records.values():
            record["blocksci_heuristic_explanation"] = explain_wasabi2_heuristic(
                record,
                min_input_count=detector.min_input_count,
                first_wasabi2_block=detector.first_wasabi2_block,
            )
        return
    if detector.coinjoin_type == "joinmarket":
        for record in records.values():
            record["blocksci_heuristic_explanation"] = explain_joinmarket_heuristic(record)
