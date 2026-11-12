#!/usr/bin/env python3
"""Render a readable Markdown report from unified_report.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporters.common import JsonObject, JsonValue, load_json, wallet_names


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def md_escape(value: JsonValue) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def short_txid(txid: str) -> str:
    return txid if len(txid) <= 16 else f"{txid[:8]}...{txid[-8:]}"


def tx_value(txid: str | None, explorer_base_url: str, *, short: bool = False) -> str:
    if not txid:
        return "-"
    return f"[{short_txid(txid) if short else txid}]({explorer_base_url.rstrip('/')}/tx/{txid})"


def sats(value: JsonValue) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float, str)):
        return f"{int(value):,}"
    return f"{int(str(value)):,}"


def summary_value(record: JsonObject | None, field: str) -> JsonValue:
    if record is None:
        return None
    return record.get(field)


def block_label(record: JsonObject | None) -> str:
    if not record or record.get("block_height") is None:
        return "-"
    return str(record["block_height"])


def block_value(record: JsonObject | None, explorer_base_url: str) -> str:
    label = block_label(record)
    if label == "-" or record is None:
        return label
    block_height = record["block_height"]
    return f"[{label}]({explorer_base_url.rstrip('/')}/block-height/{block_height})"


def denoms(record: JsonObject | None) -> str:
    if not record:
        return "-"
    denominations = record.get("repeated_output_denominations") or {}
    if not denominations:
        return "-"
    return ", ".join(f"{value}x{count}" for value, count in sorted(denominations.items()))


def wallets(record: JsonObject | None) -> str:
    """Wallets of a divergence summary (``wallets``) or of a full record (its inputs and outputs)."""
    if not record:
        return "-"
    names = record.get("wallets") or wallet_names(record)
    return ", ".join(names) if names else "-"


def table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = [
        "| " + " | ".join(md_escape(header) for header in headers) + " |",
        "| " + " | ".join("---" for _header in headers) + " |",
    ]
    lines.extend("| " + " | ".join(md_escape(value) for value in row) + " |" for row in rows)
    return lines


def section(title: str, *body: str, level: int = 2) -> list[str]:
    """A blank line, a heading and its body."""
    return ["", f"{'#' * level} {title}", "", *body]


def key_values(rows: list[list[object]], key: str = "metric") -> list[str]:
    return table([key, "value"], rows)


def transaction_record(
    report: JsonObject,
    item: JsonObject,
    source_name: str,
) -> JsonObject:
    txid = item.get("txid")
    transaction = (report.get("transactions") or {}).get(txid, {})
    return transaction.get(source_name) or item.get(source_name) or {}


def divergence_rows(kind: str, items: list[JsonObject], explorer_base_url: str) -> list[list[object]]:
    rows = []
    for item in items:
        coinjoin_analysis = item.get("coinjoin_analysis")
        blocksci = item.get("blocksci")
        txid = item.get("txid")
        rows.append(
            [
                kind,
                tx_value(txid, explorer_base_url),
                block_value(coinjoin_analysis, explorer_base_url)
                if coinjoin_analysis
                else block_value(blocksci, explorer_base_url),
                summary_value(coinjoin_analysis, "input_count"),
                summary_value(coinjoin_analysis, "output_count"),
                summary_value(blocksci, "input_count"),
                summary_value(blocksci, "output_count"),
                item.get("mismatch_count", "-"),
                item.get("reason"),
            ]
        )
    return rows


def render_shared_mismatch_details(items: list[JsonObject]) -> list[str]:
    if not items:
        return []

    lines = section("Shared Transaction Mismatch Details")
    for item in items:
        lines.append(f"### `{item.get('txid')}`")
        lines.append("")
        for mismatch in item.get("mismatches", []):
            lines.append(f"- {mismatch}")
        lines.append("")
    return lines


def render_notable_divergences(divergences: dict[str, list[JsonObject]]) -> list[str]:
    lines = section("Notable Divergences")
    added = False

    missed = divergences.get("missed_by_blocksci", [])
    if missed:
        added = True
        lines.append(f"- {len(missed)} CoinJoin reported by emulator analysis was missed by BlockSci.")
        for item in missed:
            source = item.get("coinjoin_analysis") or {}
            lines.append(
                f"- Missed transaction {item.get('txid') or '-'} is in block {block_label(source)}, "
                f"with {source.get('input_count', '-')} inputs, {source.get('output_count', '-')} outputs, "
                f"repeated denominations {denoms(source)}, wallets {wallets(source)}."
            )

    blocksci_only = divergences.get("blocksci_only", [])
    if blocksci_only:
        added = True
        lines.append(f"- {len(blocksci_only)} transaction detected by BlockSci was not reported by emulator analysis.")

    shared_mismatches = divergences.get("shared_tx_mismatches", [])
    if shared_mismatches:
        added = True
        lines.append(f"- {len(shared_mismatches)} shared transaction has normalized field mismatches.")

    if not added:
        lines.append("- No divergences found.")

    return lines


def render_source_details(
    title: str,
    items: list[JsonObject],
    source_name: str,
    explorer_base_url: str,
) -> list[str]:
    if not items:
        return []

    rows = []
    for item in items:
        source = item.get(source_name)
        txid = item.get("txid")
        rows.append(
            [
                tx_value(txid, explorer_base_url),
                block_value(source, explorer_base_url),
                summary_value(source, "input_count"),
                summary_value(source, "output_count"),
                sats(summary_value(source, "total_input_sats")),
                sats(summary_value(source, "total_output_sats")),
                denoms(source),
                wallets(source),
            ]
        )

    headers = ["txid", "block", "inputs", "outputs", "input sats", "output sats", "repeated denoms", "wallets"]
    return section(title, *table(headers, rows))


def render_transaction_details(
    title: str,
    items: list[JsonObject],
    source_name: str,
    explorer_base_url: str,
    report: JsonObject,
) -> list[str]:
    if not items:
        return []

    lines = section(f"{title} Details")
    for item in items:
        source = transaction_record(report, item, source_name)
        txid = item.get("txid")
        lines.extend(
            [
                f"### `{txid or '-'}`",
                "",
                f"- tx: {tx_value(txid, explorer_base_url)}",
                f"- block: {block_value(source, explorer_base_url)}",
                f"- wallets: {wallets(source)}",
                f"- inputs: {summary_value(source, 'input_count') or '-'}",
                f"- outputs: {summary_value(source, 'output_count') or '-'}",
                f"- input sats: {sats(summary_value(source, 'total_input_sats'))}",
                f"- output sats: {sats(summary_value(source, 'total_output_sats'))}",
                f"- repeated denominations: {denoms(source)}",
                "",
            ]
        )
        lines.extend(render_io_details("Inputs", source.get("inputs", []), explorer_base_url))
        lines.extend(render_io_details("Outputs", source.get("outputs", []), explorer_base_url))
        lines.append("")

    return lines


def render_io_details(title: str, records: list[JsonObject], explorer_base_url: str) -> list[str]:
    if not records:
        return [f"#### {title}", "", "No records.", ""]

    rows = []
    for record in records:
        rows.append(
            [
                record.get("index"),
                sats(record.get("value")),
                record.get("address"),
                record.get("wallet_name"),
                tx_value(record.get("prev_txid"), explorer_base_url, short=True),
                record.get("spending_tx"),
                record.get("spend_by_tx"),
            ]
        )

    headers = [
        "index",
        "value sats",
        "address",
        "wallet",
        "prev txid",
        "spending tx",
        "spend by tx",
    ]
    return [f"#### {title}", "", *table(headers, rows), ""]


def render_header(report: JsonObject) -> list[str]:
    return ["# BlockSci vs Emulator CoinJoin Report"]


def render_run(report: JsonObject) -> list[str]:
    run = report.get("run") or {}
    scenario = report.get("scenario") or {}
    return section(
        "Run",
        *key_values(
            [
                ["run id", run.get("id")],
                ["run time", run.get("started_at")],
                ["scenario", run.get("scenario_name")],
                ["scenario rounds", scenario.get("rounds")],
                ["scenario blocks", scenario.get("blocks")],
                ["scenario wallets", scenario.get("wallet_count")],
                ["coinjoin type", run.get("coinjoin_type")],
                ["BlockSci min input count", run.get("blocksci_min_input_count")],
                ["scenario sha256", run.get("scenario_sha256")],
            ],
            key="field",
        ),
    )


def render_detection_summary(report: JsonObject) -> list[str]:
    summary = report.get("summary") or {}
    divergence_counts = summary["divergence_counts"]
    return [
        *section(
            "Detection Summary",
            *key_values(
                [
                    ["coinjoin-analysis coinjoins", summary.get("coinjoin_analysis_coinjoins")],
                    ["BlockSci detected coinjoins", summary.get("blocksci_detected_coinjoins")],
                    ["matched by both", summary.get("matched_by_both")],
                    ["missed by BlockSci", summary.get("missed_by_blocksci")],
                    ["BlockSci only", summary.get("blocksci_only")],
                    ["BlockSci agreement rate", summary.get("blocksci_agreement_rate")],
                    [
                        "coinjoin-analysis coverage by BlockSci",
                        summary.get("coinjoin_analysis_coverage_by_blocksci"),
                    ],
                ]
            ),
        ),
        *section(
            "Divergence Counts",
            *table(
                ["type", "count"],
                [
                    ["missed by BlockSci", divergence_counts.get("missed_by_blocksci", 0)],
                    ["BlockSci only", divergence_counts.get("blocksci_only", 0)],
                    ["shared transaction mismatches", divergence_counts.get("shared_tx_mismatches", 0)],
                ],
            ),
        ),
    ]


DIVERGENCE_KINDS = (
    ("missed_by_blocksci", "missed_by_blocksci"),
    ("blocksci_only", "blocksci_only"),
    ("shared_tx_mismatch", "shared_tx_mismatches"),
)


def render_analyzer_diagnostic(report: JsonObject, explorer_base_url: str) -> list[str]:
    """The analyzer-vs-analyzer comparison, transaction by transaction."""
    divergences = report["divergences"]
    lines = section("CoinJoin-Analysis vs BlockSci Diagnostic")
    lines.extend(render_notable_divergences(divergences))
    lines.extend(section("Divergence Overview"))
    overview_rows = [
        row
        for kind, key in DIVERGENCE_KINDS
        for row in divergence_rows(kind, divergences.get(key, []), explorer_base_url)
    ]
    if overview_rows:
        lines.extend(
            table(
                [
                    "status",
                    "txid",
                    "block",
                    "coinjoin-analysis inputs",
                    "coinjoin-analysis outputs",
                    "BlockSci inputs",
                    "BlockSci outputs",
                    "mismatches",
                    "reason",
                ],
                overview_rows,
            )
        )
    else:
        lines.append("No divergences found.")

    for title, key, source_name in (
        ("Missed By BlockSci", "missed_by_blocksci", "coinjoin_analysis"),
        ("BlockSci Only", "blocksci_only", "blocksci"),
    ):
        items = divergences.get(key, [])
        lines.extend(render_source_details(title, items, source_name, explorer_base_url))
        lines.extend(render_transaction_details(title, items, source_name, explorer_base_url, report))
    lines.extend(render_shared_mismatch_details(divergences.get("shared_tx_mismatches", [])))
    return lines


def render_report(report: JsonObject, explorer_base_url: str = "http://localhost:3002") -> str:
    lines = [
        *render_header(report),
        *render_run(report),
        *render_detection_summary(report),
        *render_analyzer_diagnostic(report, explorer_base_url),
    ]
    return "\n".join(lines).rstrip() + "\n"


def default_output_path(input_path: Path) -> Path:
    return input_path.with_suffix(".md")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Markdown from a unified BlockSci-vs-emulator report.")
    parser.add_argument("input", type=Path, help="Path to unified_report.json.")
    parser.add_argument("-o", "--output", type=Path, help="Markdown output path.")
    parser.add_argument(
        "--explorer-base-url",
        default="http://localhost:3002",
        help="Base URL for transaction links.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_path = args.output or default_output_path(args.input)
    report = load_json(args.input)
    save_text(output_path, render_report(report, explorer_base_url=args.explorer_base_url))
    print(f"Markdown report saved to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
