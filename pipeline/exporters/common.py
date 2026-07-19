"""Shared JSON, subprocess, and normalization helpers for unified reports."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

JsonValue = object
JsonObject = dict

SCHEMA_VERSION = "1.7"
EMULATOR_DATA_SCHEMA_VERSION = "1.0"
SATS_IN_BTC = 100_000_000
DEFAULT_FIRST_WASABI2_BLOCK = 0
DEFAULT_CLUSTER_MAX_DISTANCE = 2
DEFAULT_JOINMARKET_DETECTOR = "definite"
DEFAULT_JOINMARKET_MIN_BASE_FEE = 5000
DEFAULT_JOINMARKET_PERCENTAGE_FEE = 0.00004
DEFAULT_JOINMARKET_MAX_DEPTH = 200000
WASABI2_THRESHOLD_CHANGE_BLOCK = 850_237
WASABI2_MAX_SATOSHIS = 134_375_000_000
WASABI2_MIN_SATOSHIS = 5_000
WASABI2_ALLOWED_ADDRESS_TYPES = {
    "WITNESS_PUBKEYHASH",
    "WITNESS_UNKNOWN",
    "witness_pubkeyhash",
    "witness_unknown",
}


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


def docker_image_digest(image: str | None) -> str | None:
    if not image:
        return None
    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                image,
                "--format",
                "{{json .RepoDigests}} {{.Id}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    output = result.stdout.strip()
    if not output:
        return None
    repo_digest_text, _sep, image_id = output.partition(" ")
    try:
        repo_digests = json.loads(repo_digest_text)
    except json.JSONDecodeError:
        repo_digests = []
    if repo_digests:
        return str(repo_digests[0])
    return image_id.strip() or None


def nested_get(data: JsonObject | None, path: tuple[str, ...]) -> JsonValue:
    current: JsonValue = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


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


def repeated_denominations(outputs: list[JsonObject]) -> dict[str, int]:
    counts = Counter(output["value"] for output in outputs if output.get("value") is not None)
    return {str(value): count for value, count in sorted(counts.items()) if count > 1}


def add_common_metrics(record: JsonObject) -> JsonObject:
    inputs = record.get("inputs", [])
    outputs = record.get("outputs", [])
    record["input_count"] = len(inputs)
    record["output_count"] = len(outputs)
    record["total_input_sats"] = sum(item["value"] or 0 for item in inputs)
    record["total_output_sats"] = sum(item["value"] or 0 for item in outputs)
    record["repeated_output_denominations"] = repeated_denominations(outputs)
    return record


def rule_result(name: str, passed: bool | None, observed: JsonValue, expected: JsonValue) -> JsonObject:
    return {
        "name": name,
        "passed": passed,
        "observed": observed,
        "expected": expected,
    }


def coerce_int(value: JsonValue) -> int | None:
    if value is None:
        return None
    return int(str(value))


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
