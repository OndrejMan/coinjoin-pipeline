"""Shared JSON, subprocess, and normalization helpers for unified reports."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from exporters.report_types import IORecord, TransactionMetrics

JsonValue = object
# Free-form JSON whose schema this code does not model: scenario files,
# emulator evidence, the rendered report. The records the report computes on
# are typed in exporters.report_types instead.
JsonObject = dict

SCHEMA_VERSION = "3.0"
SATS_IN_BTC = 100_000_000
DEFAULT_FIRST_WASABI2_BLOCK = 0


def load_json(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def save_json(path: Path, data: JsonValue) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)
        file.write("\n")


def coerce_sats(value: JsonValue, *, unit: str = "sats") -> int | None:
    """Convert an amount to integer satoshis.

    ``unit`` declares how the source encodes the amount — never inferred from the
    value, because ``1.0`` and ``1.5`` would otherwise land 10^8 apart. In
    ``"sats"`` mode a fractional amount is rejected (sats are indivisible);
    ``"btc"`` mode multiplies by 10^8.
    """
    if unit not in ("sats", "btc"):
        raise ValueError(f"coerce_sats unit must be 'sats' or 'btc', got {unit!r}")
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        number = float(stripped)
    else:
        number = float(str(value))
    if unit == "btc":
        return int(round(number * SATS_IN_BTC))
    if not number.is_integer():
        raise ValueError(f"non-integer satoshi amount: {value!r}")
    return int(number)


def to_json_text(value: JsonValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def repeated_denominations(outputs: list[IORecord]) -> dict[str, int]:
    counts = Counter(output["value"] for output in outputs if output.get("value") is not None)
    return {str(value): count for value, count in sorted(counts.items()) if count > 1}


def transaction_metrics(inputs: list[IORecord], outputs: list[IORecord]) -> TransactionMetrics:
    return {
        "input_count": len(inputs),
        "output_count": len(outputs),
        "total_input_sats": sum(item["value"] or 0 for item in inputs),
        "total_output_sats": sum(item["value"] or 0 for item in outputs),
        "repeated_output_denominations": repeated_denominations(outputs),
    }


def safe_attr(obj: object, name: str, default: object = None) -> object:
    try:
        return getattr(obj, name)
    except Exception:
        return default
