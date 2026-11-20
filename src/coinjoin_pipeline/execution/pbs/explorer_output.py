"""The explorer side of the mainnet Dumplings baseline: coinjoin-analysis output for CoinJoin
Explorer, the fin detector's liquidity curves and the manifest of the incremental upload.

``analyzer_jobs.dumplings_baseline`` runs coinjoin-analysis in its image and calls these functions
on the PBS node. Standard library only and Python 3.8 syntax, like every module a stage job ships.
"""

from __future__ import annotations

import bisect
import uuid
import gzip
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# coinjoin-analysis steps in order: the Wasabi 2 step corrects inputs that came from Wasabi 1,
# so Wasabi 1 comes first, and coordinators are detected and split only after Wasabi 2.
PARSE_STEPS = (
    ("ww1", "process_dumplings"),
    ("sw", "process_dumplings"),
    ("jm", "process_dumplings"),
    ("ww2", "process_dumplings"),
    ("ww2", "detect_coordinators"),
    ("ww2", "split_coordinators"),
)
# The Dumplings file each --cjtype reads; a protocol without coinjoins yet is skipped.
PROTOCOL_INPUTS = {"ww1": "WasabiCoinJoins.txt", "sw": "SamouraiCoinJoins.txt", "jm": "OtherCoinJoins.txt",
                   "ww2": "Wasabi2CoinJoins.txt"}
# Run in the coinjoin-analysis image: the Whirlpool pools whose funding coinjoins the Scanner holds.
# whirlpool_extract_pool indexes them unconditionally, so a pool that started later (Ashigaru,
# 2025) must be left out of MIX_IDS or the whole step fails.
WHIRLPOOL_POOLS = (
    "import json; "
    "from cj_process.cj_consts import WHIRLPOOL_FUNDING_TXS as F, WHIRLPOOL_POOL_SIZES as S; "
    "text = open('/work/dumplings/Scanner/SamouraiCoinJoins.txt').read(); "
    "print(json.dumps([n for n, size in S.items() if all(t in text for t in F[size]['funding_txs'])]))"
)
# Scanner/ directory -> the image's curated data/<protocol>/ directory it reads from.
CURATED = {"wasabi1": "wasabi1", "wasabi2": "wasabi2", "whirlpool": "whirlpool", "joinmarket_all": "joinmarket"}
CURATED_FILES = ("false_cjtxs.json", "fee_rates.json")
# Known Wasabi 2 coordinators that detect_coordinators expands; it reads them from wasabi2_others/.
COORDINATOR_SEED = ("wasabi2", "txid_coord.json", "wasabi2_others")
# The whole-protocol files the BlockSci comparison reads as its Dumplings baseline.
BASELINES = ("wasabi2", "joinmarket_all")
# coinjoin-analysis orders only Wasabi 2 by relative_order (SORT_COINJOINS_BY_RELATIVE_ORDER);
# it is the depth of the remix chain, not time, so the others are ordered by block time.
RELATIVE_ORDER_POOLS = re.compile(r"wasabi2(_.*)?")
MONTH = re.compile(r"\d{4}-\d{2}-01 00-00-00--\d{4}-\d{2}-01 00-00-00_unknown-static-100-1utxo")
FRIENDS = ("MIX_REMIX_FRIENDS", "MIX_REMIX_FRIENDS_WW1")
# The fin-detector windows baked into the second page, chosen before any mainnet run: the
# Lab's default height with the percentage of protocol v3 that removed every false window.
class FinSetting(NamedTuple):
    basis: str
    height: int
    percent: float
    start_rule: str


FIN_SETTINGS = (
    FinSetting("created", 50_000_000, 15, "strongest-jump"),
    FinSetting("spend", 50_000_000, 15, "strongest-jump"),
)
SELECTIONS_FILE = "cj_selection.json"
CURVE_SCHEMA = "fin-liquidity-1.0"
PAGE_PREFIX = "coinjoin-explorer-offline__"
MANIFEST_SCHEMA = "mainnet-explorer-1.0"


def epoch(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 24), b""):
            digest.update(block)
    return digest.hexdigest()


def gzip_file(source: Path, target: Path) -> None:
    """Without a name or time in the header, the same content always gives the same bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as raw, target.open("wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=handle, mtime=0) as packed:
            shutil.copyfileobj(raw, packed, 1 << 24)


def gzip_json(value: object, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    with target.open("wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=handle, mtime=0) as packed:
            packed.write(data)


def prepare_scanner(scanner: Path, curated: Path) -> None:
    """Put the image's curated false positives, fee rates and coordinator seed where coinjoin-analysis looks."""
    for directory, protocol in CURATED.items():
        (scanner / directory).mkdir(exist_ok=True)
        for name in CURATED_FILES:
            if (curated / protocol / name).is_file():
                shutil.copyfile(curated / protocol / name, scanner / directory / name)
    protocol, name, directory = COORDINATOR_SEED
    (scanner / directory).mkdir(exist_ok=True)
    shutil.copyfile(curated / protocol / name, scanner / directory / name)
    # detect_coordinators filters wasabi2_others with its false_cjtxs.json and fails without one.
    if (curated / protocol / "false_cjtxs.json").is_file():
        shutil.copyfile(curated / protocol / "false_cjtxs.json", scanner / directory / "false_cjtxs.json")
    # coinjoin-analysis reads <file>.json instead of a Dumplings file and never checks its age.
    for cached in scanner.glob("*.txt.json"):
        cached.unlink()


def has_coinjoins(scanner: Path, cjtype: str) -> bool:
    path = scanner / PROTOCOL_INPUTS[cjtype]
    return path.is_file() and path.stat().st_size > 0


def last_line(path: Path) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - 65536))
        lines = [line for line in handle.read().decode("utf-8", "replace").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def stop_date(scanner: Path) -> str | None:
    """Block time of the newest coinjoin in the Dumplings files, as parse_dumplings compares dates.

    Without it coinjoin-analysis ends its intervals at the time of the run, so a copy at an old
    height gets empty months up to today and the coordinators that started later.
    Dumplings appends in block order and a line's fourth field is its block time.
    """
    times = []
    for name in PROTOCOL_INPUTS.values():
        path = scanner / name
        if path.is_file() and path.stat().st_size > 0:
            parts = last_line(path).split(":::")
            if len(parts) > 3 and parts[3].strip().isdigit():
                times.append(int(parts[3]))
    if not times:
        return None
    return datetime.fromtimestamp(max(times), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def parse_options(pools: list[str] | None, stop: str | None) -> str:
    """The --env_vars option of one parse_dumplings call, or an empty string."""
    items = ([pools_option(pools)] if pools is not None else []) + ([f"interval_stop_date={stop}"] if stop else [])
    return f' --env_vars "{";".join(items)}"' if items else ""


def pools_option(pools: list[str]) -> str:
    """parse_dumplings --env_vars restricting the Whirlpool pools; its values are Python literals."""
    return "MIX_IDS=" + repr([str(pool) for pool in pools])


def months(coordinator: Path) -> list[Path]:
    return sorted(
        path
        for path in coordinator.iterdir()
        if path.is_dir() and MONTH.fullmatch(path.name) and (path / "coinjoin_tx_info.json").is_file()
    )


def coordinators(scanner: Path) -> list[str]:
    """Every directory coinjoin-analysis split into monthly intervals, as CoinJoin Explorer reads them."""
    return sorted(path.name for path in scanner.iterdir() if path.is_dir() and months(path))


def false_positives(coordinator: Path) -> set[str]:
    path = coordinator / "false_cjtxs.json"
    if not path.is_file():
        return set()
    return {txid for group in json.loads(path.read_text()).values() for txid in group}


def order_key(row: dict) -> tuple:
    return (row["time"], row["relative_order"], row["block_index"])


def compact(txid: str, tx: dict) -> dict:
    inputs, outputs = tx.get("inputs", {}).values(), tx.get("outputs", {}).values()
    values: dict[int, int] = {}
    for coin in outputs:
        values[coin["value"]] = values.get(coin["value"], 0) + 1
    return {
        "txid": txid,
        "time": epoch(tx["broadcast_time"]),
        "relative_order": tx.get("relative_order") or 0,
        "block_index": tx.get("block_index") or 0,
        "inflow": sum(c["value"] for c in inputs if c.get("mix_event_type") in ("MIX_ENTER", *FRIENDS)),
        "leaves": [(c["value"], c.get("burn_time")) for c in outputs if c.get("mix_event_type") == "MIX_LEAVE"],
        "qt": sum(count * count for count in values.values()),
    }


def coordinator_rows(coordinator: Path) -> list[dict]:
    """Every coinjoin of the coordinator except its listed false positives, in block-time order."""
    false = false_positives(coordinator)
    rows: list[dict] = []
    for month in months(coordinator):
        data = json.loads((month / "coinjoin_tx_info.json").read_text())["coinjoins"]
        rows.extend(compact(txid, tx) for txid, tx in data.items() if txid not in false)
    rows.sort(key=order_key)
    return rows


def curves(rows: list[dict]) -> dict:
    """Interim liquidity as CoinJoin Explorer counts it (created) and with each leaving output
    charged to the coordinator's first coinjoin after its spend (spend). Sample 0 is empty."""
    times = [row["time"] for row in rows]
    count = len(rows)
    spent_at = [0] * (count + 1)
    without_burn = 0
    for index, row in enumerate(rows, 1):
        for value, burn in row["leaves"]:
            if burn is None:
                spent_at[index] += value
                without_burn += 1
                continue
            later = bisect.bisect_right(times, row["time"] + float(burn)) + 1
            spent_at[index if index == count else max(index + 1, min(later, count))] += value
    created, spend = [0], [0]
    for index, row in enumerate(rows, 1):
        created.append(created[-1] + row["inflow"] - sum(value for value, _ in row["leaves"]))
        spend.append(spend[-1] + row["inflow"] - spent_at[index])
    return {"created": created, "spend": spend, "leaves_without_burn_time": without_burn}


def curve_documents(name: str, height: int, rows: list[dict]) -> dict[str, dict]:
    built = curves(rows)
    shared = {
        "schema_version": CURVE_SCHEMA,
        "provenance": {"coordinator": name, "height": height, "source": "coinjoin-analysis",
                       "leaves_without_burn_time": built["leaves_without_burn_time"]},
        "flows": {"inflowSats": [0] + [row["inflow"] for row in rows], "qt": [0] + [row["qt"] for row in rows]},
    }
    return {
        basis: {
            **shared,
            "runId": f"mainnet-{name}-{height}-{basis}",
            "publicCurve": {
                "source": "coinjoin-analysis",
                "basis": basis,
                "values": built[basis],
                "txids": [None] + [row["txid"] for row in rows],
                "times": [None] + [row["time"] for row in rows],
            },
        }
        for basis in ("created", "spend")
    }


def stage_for_explorer(scanner: Path, staging: Path) -> None:
    """CoinJoin Explorer orders by relative_order; outside Wasabi 2 that scrambles time, so the
    staged copy numbers those coinjoins chronologically. Wasabi 2 coordinators are linked as they are."""
    for name in coordinators(scanner):
        source, target = scanner / name, staging / name
        target.mkdir(parents=True)
        if (source / "false_cjtxs.json").is_file():
            shutil.copyfile(source / "false_cjtxs.json", target / "false_cjtxs.json")
        if RELATIVE_ORDER_POOLS.fullmatch(name):
            for month in months(source):
                (target / month.name).mkdir()
                link = target / month.name / "coinjoin_tx_info.json"
                # Relative, so the link holds wherever a container binds the job directory.
                os.symlink(os.path.relpath(month / "coinjoin_tx_info.json", link.parent), link)
            continue
        rank = {row["txid"]: index for index, row in enumerate(coordinator_rows(source))}
        for month in months(source):
            data = json.loads((month / "coinjoin_tx_info.json").read_text())
            for txid, tx in data["coinjoins"].items():
                # False positives are dropped by Explorer itself; they keep any order after the rest.
                tx["relative_order"] = rank.get(txid, len(rank))
            (target / month.name).mkdir()
            (target / month.name / "coinjoin_tx_info.json").write_text(json.dumps(data, separators=(",", ":")))


def explorer_months(coordinator: Path, flows: dict | None = None) -> list[tuple[str, list[str]]]:
    """Each month's coinjoins in the order CoinJoin Explorer shows them (model.py: false positives
    out, then a stable sort by relative_order), for the staged coordinator directory.

    With flows, also fills txid -> (mix enter, mix leave, liquidity before, liquidity after) in
    sats, summed as model.py does for its rows across the connected months.
    """
    false = false_positives(coordinator)
    ordered = []
    liquidity = 0
    for month in months(coordinator):
        data = json.loads((month / "coinjoin_tx_info.json").read_text())["coinjoins"]
        kept = [(txid, tx.get("relative_order") or 0) for txid, tx in data.items() if txid not in false]
        txids = [txid for txid, _ in sorted(kept, key=lambda item: item[1])]
        ordered.append((month.name, txids))
        if flows is None:
            continue
        for txid in txids:
            tx = data[txid]
            inputs, outputs = tx.get("inputs", {}).values(), tx.get("outputs", {}).values()
            enter = sum(c["value"] for c in inputs if c.get("mix_event_type") == "MIX_ENTER")
            friends = sum(c["value"] for c in inputs if c.get("mix_event_type") in FRIENDS)
            leave = sum(c["value"] for c in outputs if c.get("mix_event_type") == "MIX_LEAVE")
            before, liquidity = liquidity, liquidity + enter + friends - leave
            flows[txid] = (enter, leave, before, liquidity)
    return ordered


def month_places(ordered: list) -> dict[str, tuple[int, int]]:
    return {txid: (m, i) for m, (_, month_txids) in enumerate(ordered) for i, txid in enumerate(month_txids)}


def window_record(
    name: str, setting: FinSetting, window: object, txids: list, ordered: list, places: dict,
    flows: dict | None = None,
) -> dict | None:
    """One detector window as an Explorer saved selection, or None when Explorer shows none of it."""
    from coinjoin_pipeline.execution.pbs import fin_windows  # pylint: disable=import-outside-toplevel

    assert isinstance(window, fin_windows.Window)
    # The window holds the coinjoins after its start sample up to its end sample.
    inside = [places[txid] for txid in txids[window.start + 1 : window.end + 1] if txid in places]
    if not inside:
        return None
    (first_month, first_index), (last_month, last_index) = min(inside), max(inside)
    segments = []
    for month in range(first_month, last_month + 1):
        month_txids = ordered[month][1]
        start = first_index if month == first_month else 0
        end = last_index if month == last_month else len(month_txids) - 1
        if not month_txids or end < start:
            return None
        segments.append({"month": ordered[month][0], "start_index": start, "end_index": end,
                         "first_txid": month_txids[start], "last_txid": month_txids[end]})
    label = f"{setting.basis}, {setting.height / 1e8:g} BTC + {setting.percent:g} %, {setting.start_rule}"
    key = f"{name}:{label}:{segments[0]['first_txid']}:{segments[-1]['last_txid']}"
    record = {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "coinjoin-pipeline/fin-window/" + key)),
        "coordinator": name,
        "annotation_type": "interval",
        "description": (f"Fin detector v3 ({label}): rise {window.rise / 1e8:.2f} BTC, "
                        f"fall {window.fall / 1e8:.2f} BTC"),
        "month": segments[0]["month"],
        "start_index": segments[0]["start_index"],
        "end_index": segments[-1]["end_index"],
        "first_txid": segments[0]["first_txid"],
        "last_txid": segments[-1]["last_txid"],
        "num_coinjoins": sum(item["end_index"] - item["start_index"] + 1 for item in segments),
    }
    if len(segments) > 1:
        record["month_segments"] = segments
    if flows:
        # The fields CoinJoin Explorer stores for a selection made by hand (model.py).
        months_by_name = dict(ordered)
        selected = [
            flows[txid]
            for item in segments
            for txid in months_by_name[item["month"]][item["start_index"] : item["end_index"] + 1]
        ]
        record.update({
            "mix_enter_sats": sum(item[0] for item in selected),
            "mix_leave_sats": sum(item[1] for item in selected),
            "liquidity_before_sats": selected[0][2],
            "liquidity_after_sats": selected[-1][3],
            "liquidity_baseline": "cumulative liquidity across connected monthly datasets",
        })
    return record


def write_fin_selections(staged: Path, scanner: Path, height: int) -> dict[str, object]:
    """Write every coordinator's detector windows as its cj_selection.json; what was found."""
    from coinjoin_pipeline.execution.pbs import fin_windows  # pylint: disable=import-outside-toplevel

    found: dict[str, object] = {}
    for name in coordinators(staged):
        documents = curve_documents(name, height, coordinator_rows(scanner / name))
        flows: dict = {}
        ordered = explorer_months(staged / name, flows)
        places = month_places(ordered)
        records: list[dict] = []
        counts: dict[str, int | str] = {}
        for setting in FIN_SETTINGS:
            curve = documents[setting.basis]["publicCurve"]
            label = f"{setting.basis}:{setting.percent:g}"
            if len(curve["values"]) > fin_windows.MAX_SAMPLES:
                counts[label] = "over sample limit"
                continue
            windows = fin_windows.detect(curve["values"], setting.height, 2, setting.percent, setting.start_rule)
            built = (window_record(name, setting, window, curve["txids"], ordered, places, flows) for window in windows)
            kept = [record for record in built if record is not None]
            counts[label] = len(kept)
            records.extend(kept)
        (staged / name / SELECTIONS_FILE).write_text(json.dumps(records, indent=1))
        found[name] = counts
    return {"algorithm": fin_windows.ALGORITHM, "settings": [s._asdict() for s in FIN_SETTINGS], "windows": found}


def build_output(scanner: Path, out: Path, height: int, html: Path | None, html_fins: Path | None = None) -> None:
    """The tree the job uploads: per-coordinator months, curves, the comparison baselines and the page."""
    for name in coordinators(scanner):
        source = scanner / name
        for month in months(source):
            gzip_file(month / "coinjoin_tx_info.json", out / "data" / name / month.name / "coinjoin_tx_info.json.gz")
        if (source / "false_cjtxs.json").is_file():
            shutil.copyfile(source / "false_cjtxs.json", out / "data" / name / "false_cjtxs.json")
        for basis, document in curve_documents(name, height, coordinator_rows(source)).items():
            gzip_json(document, out / "curves" / f"{name}.{basis}.json.gz")
    for name in BASELINES:
        if (scanner / name / "coinjoin_tx_info.json").is_file():
            gzip_file(scanner / name / "coinjoin_tx_info.json", out / "baseline" / name / "coinjoin_tx_info.json.gz")
            if (scanner / name / "false_cjtxs.json").is_file():
                shutil.copyfile(scanner / name / "false_cjtxs.json", out / "baseline" / name / "false_cjtxs.json")
    # One self-contained page per coordinator; the export's "_all" pages repeat them all.
    for directory, folder in ((html, "html"), (html_fins, "html-fins")):
        if directory is not None:
            for page in sorted(directory.glob(PAGE_PREFIX + "*.html")):
                gzip_file(page, out / folder / (page.name + ".gz"))


def files(root: Path) -> dict[str, dict]:
    return {
        str(path.relative_to(root)): {"size": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def changes(previous: dict, current: dict) -> tuple[list[str], list[str]]:
    """What to upload (new or different content) and what to delete, against the previous manifest."""
    old = previous.get("files") if previous.get("schema_version") == MANIFEST_SCHEMA else None
    old = old if isinstance(old, dict) else {}
    upload = [name for name, item in current.items() if (old.get(name) or {}).get("sha256") != item["sha256"]]
    return upload, sorted(set(old) - set(current))


def scanner_inputs(scanner: Path) -> dict[str, int]:
    """The Dumplings files the job read, for the manifest and a later skip of unchanged protocols."""
    return {path.name: path.stat().st_size for path in sorted(scanner.glob("*.txt"))}


def iter_commands(uploads: list[str], deletions: list[str], out: Path, prefix: str) -> Iterator[str]:
    """One s5cmd run file: copies in parallel, then removals of what no longer exists."""
    for name in uploads:
        yield f'cp "{out / name}" "{prefix}/{name}"'
    for name in deletions:
        yield f'rm "{prefix}/{name}"'
