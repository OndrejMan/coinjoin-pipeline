#!/usr/bin/env python3
"""Render a readable Markdown report from unified_report.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

JsonValue = object
JsonObject = dict


def load_json(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def md_escape(value: JsonValue) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def format_txid(txid: str | None) -> str:
    if not txid:
        return "-"
    return txid


def short_txid(txid: str | None) -> str:
    if not txid:
        return "-"
    if len(txid) <= 16:
        return txid
    return f"{txid[:8]}...{txid[-8:]}"


def tx_value(txid: str | None, explorer_base_url: str) -> str:
    if not txid:
        return "-"
    return f"[{txid}]({explorer_base_url.rstrip('/')}/tx/{txid})"


def tx_ref(value: str | None, explorer_base_url: str) -> str:
    if not value:
        return "-"
    return f"[{short_txid(value)}]({explorer_base_url.rstrip('/')}/tx/{value})"


def sats(value: JsonValue) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float, str)):
        return f"{int(value):,}"
    return f"{int(str(value)):,}"


def metric_value(value: JsonValue) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def summary_value(record: JsonObject | None, field: str) -> JsonValue:
    if record is None:
        return None
    return record.get(field)


def block_label(record: JsonObject | None) -> str:
    if not record or record.get("block_height") is None:
        return "-"
    prefix = "_" if record.get("block_height_inferred") else ""
    return f"{prefix}{record['block_height']}"


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
    if not record:
        return "-"
    names = record.get("wallets") or []
    if not names:
        names = sorted(
            {
                item["wallet_name"]
                for side in ("inputs", "outputs")
                for item in record.get(side, [])
                if item.get("wallet_name")
            }
        )
    return ", ".join(names) if names else "-"


def table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = [
        "| " + " | ".join(md_escape(header) for header in headers) + " |",
        "| " + " | ".join("---" for _header in headers) + " |",
    ]
    lines.extend("| " + " | ".join(md_escape(value) for value in row) + " |" for row in rows)
    return lines


def rule_passed_text(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def rule_cell(value: JsonValue) -> JsonValue:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return value


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

    lines = ["", "## Shared Transaction Mismatch Details", ""]
    for item in items:
        lines.append(f"### `{item.get('txid')}`")
        lines.append("")
        for mismatch in item.get("mismatches", []):
            lines.append(f"- {mismatch}")
        lines.append("")
    return lines


def render_emulator_detection(report: JsonObject, explorer_base_url: str) -> list[str]:
    emulator_data = report.get("emulator_data") or {}
    emulator_summary = emulator_data.get("summary") or {}
    matrix = report.get("detection_confusion_matrix")
    if not matrix:
        return []

    lines = [
        "",
        "## Emulator Data Coverage",
        "",
        *table(
            ["metric", "value"],
            [
                ["chain transactions evaluated", emulator_summary.get("transactions")],
                ["emulator_data coinjoins", emulator_summary.get("coinjoin_transactions")],
                ["emulator_data non-coinjoins", emulator_summary.get("non_coinjoin_transactions")],
                ["unknown transactions", emulator_summary.get("unknown_transactions")],
                ["wallet addresses", emulator_summary.get("wallet_addresses")],
                ["labeled IO records", emulator_summary.get("labeled_io_records")],
                ["total IO records", emulator_summary.get("total_io_records")],
            ],
        ),
        "",
        "## Emulator Data Detection Confusion Matrix",
        "",
        *table(
            ["metric", "value"],
            [
                ["true positives", matrix.get("true_positives")],
                ["false positives", matrix.get("false_positives")],
                ["false negatives", matrix.get("false_negatives")],
                ["true negatives", matrix.get("true_negatives")],
                ["unknown", matrix.get("unknown")],
                ["precision", metric_value(matrix.get("precision"))],
                ["recall", metric_value(matrix.get("recall"))],
                ["F1", metric_value(matrix.get("f1"))],
                ["specificity", metric_value(matrix.get("specificity"))],
                ["false positive rate", metric_value(matrix.get("false_positive_rate"))],
            ],
        ),
    ]

    false_negative_txids = matrix.get("false_negative_txids") or []
    false_positive_txids = matrix.get("false_positive_txids") or []
    if false_negative_txids or false_positive_txids:
        lines.extend(["", "## Emulator Data Detection Divergences", ""])
        if false_negative_txids:
            lines.append(f"- {len(false_negative_txids)} emulator_data CoinJoin was missed by BlockSci.")
            for txid in false_negative_txids[:10]:
                lines.append(f"- false negative: {tx_value(txid, explorer_base_url)}")
        if false_positive_txids:
            lines.append(f"- {len(false_positive_txids)} non-CoinJoin transaction was detected by BlockSci.")
            for txid in false_positive_txids[:10]:
                lines.append(f"- false positive: {tx_value(txid, explorer_base_url)}")

    return lines


def render_clustering_evaluation(report: JsonObject) -> list[str]:
    clustering = report.get("clustering_evaluation")
    if not clustering:
        return []

    lines = [
        "",
        "## Clustering Evaluation",
        "",
        *table(
            ["metric", "value"],
            [
                ["available", clustering.get("available")],
                ["labeled addresses", clustering.get("labeled_addresses")],
                ["clustered labeled addresses", clustering.get("clustered_labeled_addresses")],
                ["unclustered labeled addresses", clustering.get("unclustered_labeled_addresses")],
                ["pairwise precision", metric_value(clustering.get("pairwise_precision"))],
                ["pairwise recall", metric_value(clustering.get("pairwise_recall"))],
                ["pairwise F1", metric_value(clustering.get("pairwise_f1"))],
                ["overmerged clusters", clustering.get("overmerged_clusters")],
                ["undermerged wallets", clustering.get("undermerged_wallets")],
            ],
        ),
    ]
    if not clustering.get("available"):
        lines.extend(["", clustering.get("reason") or "BlockSci clustering data is not available for this run."])
        return lines

    overmerged = clustering.get("largest_overmerged_clusters") or []
    if overmerged:
        lines.extend(
            [
                "",
                "### Largest Overmerged Clusters",
                "",
                *table(
                    ["cluster", "wallets", "wallet count"],
                    [
                        [item.get("cluster"), ", ".join(item.get("wallets") or []), item.get("wallet_count")]
                        for item in overmerged
                    ],
                ),
            ]
        )

    undermerged = clustering.get("largest_undermerged_wallets") or []
    if undermerged:
        lines.extend(
            [
                "",
                "### Largest Undermerged Wallets",
                "",
                *table(
                    ["wallet", "clusters", "cluster count"],
                    [
                        [item.get("wallet"), ", ".join(item.get("clusters") or []), item.get("cluster_count")]
                        for item in undermerged
                    ],
                ),
            ]
        )

    return lines


def render_skipped_transactions(report: JsonObject, explorer_base_url: str) -> list[str]:
    skipped_txids = report.get("blocksci_skipped_txids") or []
    if not skipped_txids:
        return []

    return [
        "",
        "## BlockSci Skipped Transactions",
        "",
        "These transactions hit the JoinMarket detector timeout or max-depth limit and are not counted as "
        "ordinary false negatives.",
        "",
        *table(
            ["txid"],
            [[tx_value(txid, explorer_base_url)] for txid in skipped_txids],
        ),
    ]


def render_notable_divergences(divergences: dict[str, list[JsonObject]]) -> list[str]:
    lines = ["", "## Notable Divergences", ""]
    added = False

    missed = divergences.get("missed_by_blocksci", [])
    if missed:
        added = True
        lines.append(f"- {len(missed)} CoinJoin reported by emulator analysis was missed by BlockSci.")
        for item in missed:
            source = item.get("coinjoin_analysis") or {}
            txid = item.get("txid")
            block = block_label(source)
            repeated_denoms = denoms(source)
            wallets_text = wallets(source)
            lines.append(
                f"- Missed transaction {format_txid(txid)} is in block {block}, "
                f"with {source.get('input_count', '-')} inputs, {source.get('output_count', '-')} outputs, "
                f"repeated denominations {repeated_denoms}, wallets {wallets_text}."
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

    return [
        "",
        f"## {title}",
        "",
        *table(
            [
                "txid",
                "block",
                "inputs",
                "outputs",
                "input sats",
                "output sats",
                "repeated denoms",
                "wallets",
            ],
            rows,
        ),
    ]


def render_transaction_details(
    title: str,
    items: list[JsonObject],
    source_name: str,
    explorer_base_url: str,
    report: JsonObject,
) -> list[str]:
    if not items:
        return []

    lines = ["", f"## {title} Details", ""]
    for item in items:
        source = transaction_record(report, item, source_name)
        txid = item.get("txid")
        lines.extend(
            [
                f"### `{format_txid(txid)}`",
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
        lines.extend(render_heuristic_explanation(source, source_name == "coinjoin_analysis" and "Missed" in title))
        lines.extend(render_io_details("Inputs", source.get("inputs", []), explorer_base_url))
        lines.extend(render_io_details("Outputs", source.get("outputs", []), explorer_base_url))
        lines.append("")

    return lines


def render_heuristic_explanation(source: JsonObject, missed_by_blocksci: bool) -> list[str]:
    explanation = source.get("blocksci_heuristic_explanation") or {}
    if not explanation:
        return []

    failed_rules = explanation.get("failed_rules") or []
    mirror_result = "pass" if explanation.get("would_pass_python_rules") else "fail"
    failed_text = ", ".join(failed_rules) if failed_rules else "-"
    rows = [
        [
            rule.get("name"),
            rule_passed_text(rule.get("passed")),
            rule_cell(rule.get("observed")),
            rule_cell(rule.get("expected")),
        ]
        for rule in explanation.get("rules", [])
    ]
    lines = [
        "#### BlockSci Heuristic Explanation",
        "",
        f"- Python mirror result: {mirror_result}",
        f"- Failed rules: {failed_text}",
        "",
        *table(["rule", "passed", "observed", "expected"], rows),
        "",
    ]
    if record_has_taproot_witness_unknown(source):
        lines.extend(
            [
                "Note: BlockSci classifies taproot / witness v1 outputs as WITNESS_UNKNOWN in this build. "
                "This is expected and is allowed by the Wasabi2 heuristic.",
                "",
            ]
        )
    if missed_by_blocksci and explanation.get("would_pass_python_rules"):
        heuristic = explanation.get("heuristic")
        if heuristic in {"joinmarket_definite", "joinmarket_possible"}:
            lines.extend(
                [
                    "Python mirror passes; likely difference is runtime BlockSci image/cache mismatch or "
                    "unavailable raw BlockSci transaction state. Re-run with a pinned local BlockSci image to "
                    "confirm.",
                    "",
                ]
            )
            return lines
        lines.extend(
            [
                "Python mirror passes; likely difference is unavailable address-type data, denomination set "
                "mismatch, or BlockSci configuration/threshold behavior.",
                "",
            ]
        )
    return lines


def record_has_taproot_witness_unknown(source: JsonObject) -> bool:
    return any(
        item.get("script_type") == "witness_v1_taproot" and item.get("address_type") == "WITNESS_UNKNOWN"
        for side in ("inputs", "outputs")
        for item in source.get(side, [])
    )


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
                record.get("script_type"),
                record.get("address_type"),
                record.get("wallet_name"),
                tx_ref(record.get("prev_txid"), explorer_base_url),
                record.get("spending_tx"),
                record.get("spend_by_tx"),
            ]
        )

    return [
        f"#### {title}",
        "",
        *table(
            [
                "index",
                "value sats",
                "address",
                "script type",
                "BlockSci type",
                "wallet",
                "prev txid",
                "spending tx",
                "spend by tx",
            ],
            rows,
        ),
        "",
    ]


def render_run_manifest(report: JsonObject) -> list[str]:
    manifest = report.get("run_manifest") or {}
    if not manifest:
        return []

    scenario = manifest.get("scenario") or {}
    execution = manifest.get("execution") or {}
    detector = manifest.get("detector") or {}
    images = manifest.get("images") or {}
    image_digests = manifest.get("image_digests") or {}
    commits = manifest.get("source_commits") or {}
    comparison = report.get("run_manifest_comparison") or {}

    lines = [
        "",
        "## Run Manifest",
        "",
        *table(
            ["field", "value"],
            [
                ["scenario sha256", scenario.get("sha256")],
                ["engine", execution.get("engine")],
                ["coinjoin type", execution.get("coinjoin_type")],
                ["detector", json.dumps(detector, sort_keys=True)],
                ["BlockSci image", images.get("blocksci")],
                ["BlockSci image digest", image_digests.get("blocksci")],
                ["coinjoin-analysis image", images.get("coinjoin_analysis")],
                ["coinjoin-analysis image digest", image_digests.get("coinjoin_analysis")],
                ["coinjoin-emulator image", images.get("coinjoin_emulator")],
                ["coinjoin-emulator image digest", image_digests.get("coinjoin_emulator")],
                ["wrapper image", images.get("wrapper")],
                ["wrapper image digest", image_digests.get("wrapper")],
                ["coinjoin-emulator commit", commits.get("coinjoin_emulator")],
                ["exporters commit", commits.get("exporters")],
            ],
        ),
    ]

    if comparison.get("available"):
        differences = comparison.get("differences") or []
        lines.extend(["", "## Run Manifest Comparison", ""])
        if not differences:
            lines.append("No tracked manifest fields changed since the previous report.")
        else:
            lines.extend(
                table(
                    ["field", "previous", "current"],
                    [
                        [
                            item.get("field"),
                            json.dumps(item.get("previous"), sort_keys=True),
                            json.dumps(item.get("current"), sort_keys=True),
                        ]
                        for item in differences
                    ],
                )
            )

    return lines


def status_text(value: JsonValue) -> str:
    if value == "ok":
        return "OK"
    if value == "not_ok":
        return "NOT OK"
    if value == "unavailable":
        return "unavailable"
    return metric_value(value)


def render_integration_diagnostics(report: JsonObject) -> list[str]:
    diagnostics = report.get("integration_diagnostics") or {}
    if not diagnostics:
        return [
            "",
            "## Inner Report Integration",
            "",
            "Inner report integration is NOT OK.",
            "",
            "No integration diagnostics are available in this report.",
        ]

    images = diagnostics.get("images") or {}
    chain = diagnostics.get("chain") or {}
    target_txids = diagnostics.get("target_txids") or {}
    detector = diagnostics.get("detector") or {}
    image_status = "ok" if images and all((item or {}).get("status") == "ok" for item in images.values()) else "not_ok"
    problems = diagnostics.get("problems") or []
    verdict = (
        "Inner report integration is OK."
        if diagnostics.get("status") == "ok"
        else "Inner report integration is NOT OK."
    )
    lines = [
        "",
        "## Inner Report Integration",
        "",
        verdict,
        "",
        *table(
            ["check", "status", "details"],
            [
                [
                    "image provenance",
                    status_text(image_status),
                    (
                        f"{sum(1 for item in images.values() if (item or {}).get('status') == 'ok')}"
                        f"/{len(images)} images complete"
                    ),
                ],
                [
                    "chain heights",
                    status_text(chain.get("status")),
                    (
                        f"BlockSci height {chain.get('blocksci_chain_height')}; "
                        f"exported max height {chain.get('max_exported_block_height')}"
                    ),
                ],
                [
                    "target txids",
                    status_text(target_txids.get("status")),
                    (
                        f"{target_txids.get('present')}/{target_txids.get('total')} present; "
                        f"{target_txids.get('height_mismatches')} height mismatches"
                    ),
                ],
                [
                    "detector check",
                    status_text(detector.get("status")),
                    (
                        f"{detector.get('checked')} checked; "
                        f"{detector.get('disagreements')} disagreements; "
                        f"{detector.get('timeouts')} timeouts"
                    ),
                ],
            ],
        ),
    ]
    if problems:
        lines.extend(["", "Blocking problems:"])
        lines.extend(f"- {problem}" for problem in problems)
    return lines


def render_coinjoin_mappings(report: JsonObject) -> list[str]:
    mappings = report.get("coinjoin_mappings") or {}
    if not mappings:
        return []
    enumerator = mappings.get("enumerator") or {}
    enum_summary = enumerator.get("summary") or {}
    sake = mappings.get("sake") or {}
    sake_summary = sake.get("summary") or {}
    lines = ["", "## CoinJoin Mapping Analysis", "", *table(
        ["metric", "value"],
        [["stage status", mappings.get("status")],
         ["enumerated transactions", enum_summary.get("transactions")],
         ["completed", enum_summary.get("completed")],
         ["timed out", enum_summary.get("timed_out")],
         ["errors", enum_summary.get("errors")],
         ["Sake seed", sake.get("seed")],
         ["Sake output match rate", metric_value(sake_summary.get("output_match_rate"))],
         ["Sake wallet match rate", metric_value(sake_summary.get("wallet_match_rate"))],
         ["Sake length match rate", metric_value(sake_summary.get("length_match_rate"))],
         ["Sake full CoinJoin match rate", metric_value(sake_summary.get("full_coinjoin_match_rate"))]],
    ), "", "### Per-transaction mapping results", ""]
    rows = []
    sake_transactions = sake.get("transactions") or {}
    for txid, item in sorted((enumerator.get("transactions") or {}).items()):
        sake_item = sake_transactions.get(txid) or {}
        rows.append([txid, item.get("status"), item.get("mapping_count"), item.get("retried"),
                     sake_item.get("matched_outputs"), sake_item.get("total_outputs"),
                     sake_item.get("full_coinjoin_match")])
    lines.extend(table(["txid", "status", "mappings", "retried", "Sake matched outputs",
                        "Sake total outputs", "Sake full match"], rows) if rows else ["No transactions."])
    return lines


def render_report(report: JsonObject, explorer_base_url: str = "http://localhost:3002") -> str:
    run = report.get("run") or {}
    scenario = report.get("scenario") or {}
    summary = report.get("summary") or {}
    divergences = report["divergences"]
    divergence_counts = summary["divergence_counts"]

    lines = [
        "# BlockSci CoinJoin Comparison Report"
        if (report.get("run") or {}).get("mode") == "external"
        else "# BlockSci vs Emulator CoinJoin Report",
        "",
        "> `_N` means the block height was inferred from exported `block_*.json` files, not present in original "
        "`coinjoin_tx_info.json`.",
    ]
    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(["", "## Warnings", ""])
        for warning in warnings:
            lines.append(f"> **{warning.get('code')}:** {warning.get('message')}")
    lines.extend(render_integration_diagnostics(report))
    lines.extend([
        "",
        "## Run",
        "",
        *table(
            ["field", "value"],
            [
                ["run id", run.get("id")],
                ["mode", run.get("mode")],
                ["network", run.get("network")],
                ["evaluation scope", report.get("evaluation_scope")],
                ["run time", run.get("started_at")],
                ["scenario", run.get("scenario_name")],
                ["scenario rounds", scenario.get("rounds")],
                ["scenario blocks", scenario.get("blocks")],
                ["scenario wallets", scenario.get("wallet_count")],
                ["coinjoin type", run.get("coinjoin_type")],
                ["BlockSci min input count", run.get("blocksci_min_input_count")],
                ["BlockSci test values", run.get("blocksci_test_values")],
                ["FirstWasabi2Block", run.get("first_wasabi2_block")],
                ["JoinMarket detector", run.get("joinmarket_detector")],
                ["JoinMarket min base fee", run.get("joinmarket_min_base_fee")],
                ["JoinMarket percentage fee", run.get("joinmarket_percentage_fee")],
                ["JoinMarket max depth", run.get("joinmarket_max_depth")],
                ["scenario sha256", run.get("scenario_sha256")],
            ],
        ),
        "",
        "## Detection Summary",
        "",
        *table(
            ["metric", "value"],
            [
                ["coinjoin-analysis coinjoins", summary.get("coinjoin_analysis_coinjoins")],
                ["BlockSci detected coinjoins", summary.get("blocksci_detected_coinjoins")],
                ["matched by both", summary.get("matched_by_both")],
                ["missed by BlockSci", summary.get("missed_by_blocksci")],
                ["BlockSci only", summary.get("blocksci_only")],
                ["BlockSci JoinMarket skipped", summary.get("blocksci_joinmarket_skipped")],
                ["BlockSci agreement rate", summary.get("blocksci_agreement_rate")],
                [
                    "coinjoin-analysis coverage by BlockSci",
                    summary.get("coinjoin_analysis_coverage_by_blocksci"),
                ],
            ],
        ),
        "",
        "## Divergence Counts",
        "",
        *table(
            ["type", "count"],
            [
                ["missed by BlockSci", divergence_counts.get("missed_by_blocksci", 0)],
                ["BlockSci only", divergence_counts.get("blocksci_only", 0)],
                ["shared transaction mismatches", divergence_counts.get("shared_tx_mismatches", 0)],
            ],
        ),
    ])

    if report.get("evaluation_scope") == "baseline_agreement_only":
        lines.extend([
            "",
            "> This report compares BlockSci with `coinjoin-analysis`. It has no emulator ground truth; "
            "precision, recall, and F1 are intentionally unavailable.",
        ])
    elif report.get("evaluation_scope") == "emulator_labels_unavailable":
        label_provenance = (report.get("emulator_data") or {}).get("label_provenance") or {}
        unavailable_reason = label_provenance.get("unavailable_reason")
        lines.extend([
            "",
            "> Independent emulator producer labels were unavailable. Transaction labels remain unknown; "
            "precision, recall, and F1 are intentionally unavailable.",
        ])
        if unavailable_reason:
            lines.append(f"> Reason: {unavailable_reason}")
    baseline_filter = report.get("baseline_filter") or {}
    if baseline_filter.get("enabled"):
        source_names = ", ".join(
            str(source.get("file")) for source in baseline_filter.get("sources", [])
        )
        lines.extend([
            "",
            "## Baseline False-Positive Filter",
            "",
            *table(
                ["field", "value"],
                [
                    ["source files", source_names],
                    ["listed unique TXIDs", baseline_filter.get("listed_txids")],
                    ["filtered baseline TXIDs", baseline_filter.get("filtered_count")],
                ],
            ),
        ])
    lines.extend(render_run_manifest(report))
    lines.extend(render_coinjoin_mappings(report))
    lines.extend(render_emulator_detection(report, explorer_base_url))
    lines.extend(render_skipped_transactions(report, explorer_base_url))
    lines.extend(render_clustering_evaluation(report))
    lines.extend(
        [
            "",
            "## CoinJoin-Analysis vs BlockSci Diagnostic",
            "",
            "This section preserves the analyzer comparison. Emulator reports use `emulator_data` as the "
            "canonical source for chain-wide detection metrics; external reports measure baseline agreement only.",
        ]
    )

    overview_rows = []
    overview_rows.extend(
        divergence_rows("missed_by_blocksci", divergences.get("missed_by_blocksci", []), explorer_base_url)
    )
    overview_rows.extend(divergence_rows("blocksci_only", divergences.get("blocksci_only", []), explorer_base_url))
    overview_rows.extend(
        divergence_rows("shared_tx_mismatch", divergences.get("shared_tx_mismatches", []), explorer_base_url)
    )

    lines.extend(render_notable_divergences(divergences))
    lines.extend(["", "## Divergence Overview", ""])

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

    lines.extend(
        render_source_details(
            "Missed By BlockSci",
            divergences.get("missed_by_blocksci", []),
            "coinjoin_analysis",
            explorer_base_url,
        )
    )
    lines.extend(
        render_transaction_details(
            "Missed By BlockSci",
            divergences.get("missed_by_blocksci", []),
            "coinjoin_analysis",
            explorer_base_url,
            report,
        )
    )
    lines.extend(
        render_source_details(
            "BlockSci Only",
            divergences.get("blocksci_only", []),
            "blocksci",
            explorer_base_url,
        )
    )
    lines.extend(
        render_transaction_details(
            "BlockSci Only",
            divergences.get("blocksci_only", []),
            "blocksci",
            explorer_base_url,
            report,
        )
    )
    lines.extend(render_shared_mismatch_details(divergences.get("shared_tx_mismatches", [])))

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
