"""Shared JSON, subprocess, and normalization helpers for unified reports."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from collections.abc import Iterator, Mapping
from datetime import datetime
from functools import partial
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
EMULATOR_DATA_SCHEMA_VERSION = "1.0"
SATS_IN_BTC = 100_000_000
DEFAULT_FIRST_WASABI2_BLOCK = 0
WASABI2_THRESHOLD_CHANGE_BLOCK = 850_237


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


def canonical_json(data: JsonValue) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def sha256_json(data: JsonValue) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def first_present(*values: str | None) -> str | None:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def git_commit_for_path(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(partial(stream.read, 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_run_started_at(run_id: str) -> str | None:
    try:
        return datetime.strptime(run_id[:16], "%Y-%m-%d_%H-%M").isoformat()
    except ValueError:
        return None


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


def sorted_items(mapping: JsonObject) -> list[tuple[str, JsonObject]]:
    def key(item: tuple[str, JsonObject]) -> tuple[int, int | str]:
        raw_key = item[0]
        try:
            return (0, int(raw_key))
        except (TypeError, ValueError):
            return (1, raw_key)

    return sorted(mapping.items(), key=key)


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


def compute_rate(numerator: int, denominator: int, empty_default: float) -> float:
    if denominator == 0:
        return empty_default
    return round(numerator / denominator, 6)


def compute_optional_rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def f1_score(precision: float | None, recall: float | None) -> float | None:
    """Harmonic mean of two rates, rounded like them; None when it is undefined."""
    if precision is None or recall is None or precision + recall <= 0:
        return None
    return round(2 * precision * recall / (precision + recall), 6)


IO_SIDES = ("inputs", "outputs")


def io_items(record: Mapping[str, object] | None) -> Iterator[Mapping[str, object]]:
    """Every input then every output of a transaction record that keeps them as lists."""
    for side in IO_SIDES:
        items = (record or {}).get(side)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping):
                    yield item


def wallet_names(record: Mapping[str, object] | None) -> list[str]:
    """Sorted wallets owning any input or output of ``record``."""
    return sorted({str(item["wallet_name"]) for item in io_items(record) if item.get("wallet_name")})
