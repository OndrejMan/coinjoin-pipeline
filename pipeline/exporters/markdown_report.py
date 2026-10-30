#!/usr/bin/env python3
"""Render a readable Markdown report from unified_report.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporters.common import JsonObject, JsonValue, io_items, load_json, wallet_names


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

    lines = section("Shared Transaction Mismatch Details")
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
    detector_evaluations = report.get("detector_evaluations")
    if not detector_evaluations:
        legacy_matrix = report.get("detection_confusion_matrix")
        detector_evaluations = {"blocksci": legacy_matrix} if legacy_matrix else None
    if not detector_evaluations:
        return []

    detector_labels = {
        "blocksci": "BlockSci",
        "coinjoin_analysis": "coinjoin-analysis",
    }
    evaluation_rows = []
    for detector, matrix in detector_evaluations.items():
        if not matrix:
            continue
        evaluation_rows.append(
            [
                detector_labels.get(detector, detector),
                matrix.get("true_positives"),
                matrix.get("false_positives"),
                matrix.get("true_negatives"),
                matrix.get("false_negatives"),
                matrix.get("unknown"),
                metric_value(matrix.get("precision")),
                metric_value(matrix.get("recall")),
                metric_value(matrix.get("f1")),
                metric_value(matrix.get("specificity")),
                metric_value(matrix.get("false_positive_rate")),
            ]
        )

    lines = [
        *section(
            "Emulator Data Coverage",
            *key_values(
                [
                    ["chain transactions evaluated", emulator_summary.get("transactions")],
                    ["emulator_data coinjoins", emulator_summary.get("coinjoin_transactions")],
                    ["emulator_data non-coinjoins", emulator_summary.get("non_coinjoin_transactions")],
                    ["unknown transactions", emulator_summary.get("unknown_transactions")],
                    ["wallet addresses", emulator_summary.get("wallet_addresses")],
                    ["labeled IO records", emulator_summary.get("labeled_io_records")],
                    ["total IO records", emulator_summary.get("total_io_records")],
                ]
            ),
        ),
        *section(
            "Emulator Data Detection Confusion Matrix",
            *table(
                ["detector", "TP", "FP", "TN", "FN", "unknown", "precision", "recall", "F1", "specificity", "FPR"],
                evaluation_rows,
            ),
        ),
    ]

    if "coinjoin_analysis" in detector_evaluations:
        lines.extend(
            [
                "",
                "> `coinjoin-analysis` metrics use the normalized baseline after applying the "
                "false-positive sidecar files recorded in this report.",
            ]
        )

    divergence_lines = []
    for detector, matrix in detector_evaluations.items():
        if not matrix:
            continue
        detector_label = detector_labels.get(detector, detector)
        for field, kind, description in (
            ("false_negative_txids", "false negative", "emulator_data CoinJoin transaction(s) missed."),
            ("false_positive_txids", "false positive", "non-CoinJoin transaction(s) detected."),
            (
                "out_of_scope_detected_txids",
                "out of scope",
                "detected transaction(s) were outside the exported emulator transaction universe.",
            ),
        ):
            txids = matrix.get(field) or []
            if txids:
                divergence_lines.append(f"- {detector_label}: {len(txids)} {description}")
                divergence_lines.extend(f"  - {kind}: {tx_value(txid, explorer_base_url)}" for txid in txids[:10])

    if divergence_lines:
        lines.extend(section("Emulator Data Detection Divergences", *divergence_lines))

    return lines


def render_clustering_evaluation(report: JsonObject) -> list[str]:
    clustering = report.get("clustering_evaluation")
    if not clustering:
        return []

    lines = section(
        "Clustering Evaluation",
        *key_values(
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
            ]
        ),
    )
    if not clustering.get("available"):
        lines.extend(["", clustering.get("reason") or "BlockSci clustering data is not available for this run."])
        return lines

    for title, field, name, members, count in (
        ("Largest Overmerged Clusters", "largest_overmerged_clusters", "cluster", "wallets", "wallet_count"),
        ("Largest Undermerged Wallets", "largest_undermerged_wallets", "wallet", "clusters", "cluster_count"),
    ):
        items = clustering.get(field) or []
        if items:
            rows = [[item.get(name), ", ".join(item.get(members) or []), item.get(count)] for item in items]
            lines.extend(section(title, *table([name, members, count.replace("_", " ")], rows), level=3))
    return lines


def is_disagreement(confusion_key: str) -> bool:
    """Whether a ``side:TRUTH->ANALYZER`` confusion key pairs two different events."""
    truth, _, analyzer = confusion_key.partition("->")
    return truth.partition(":")[2] != analyzer


def render_liquidity_evaluation(report: JsonObject) -> list[str]:
    liquidity = report.get("liquidity_evaluation")
    if not liquidity:
        return []
    lines = section("Liquidity Evaluation")
    if not liquidity.get("available"):
        lines.append(liquidity.get("reason") or "Liquidity evaluation is not available for this run.")
        return lines

    ground_truth = liquidity.get("ground_truth") or {}
    observed = liquidity.get("observed") or {}
    agreement = liquidity.get("classification_agreement") or {}
    blocksci = liquidity.get("blocksci") or {}
    with_blocksci = blocksci.get("available") is True
    blocksci_agreement = blocksci.get("classification_agreement") or {}
    lines.extend(
        table(
            ["metric", "ground truth", "coinjoin-analysis", *(["BlockSci"] if with_blocksci else [])],
            [
                [
                    name,
                    sats(ground_truth.get(field)),
                    sats(observed.get(field)),
                    *([sats(blocksci.get(field))] if with_blocksci else []),
                ]
                for name, field in (
                    ("entered", "entered_sats"),
                    ("left", "left_sats"),
                    ("stayed", "stayed_sats"),
                    ("peak interim liquidity", "peak_interim_sats"),
                    ("final interim liquidity", "final_interim_sats"),
                )
            ],
        )
    )
    summary_rows: list[list[object]] = [
        ["coinjoins", liquidity.get("coinjoins")],
        ["max interim difference", sats(liquidity.get("interim_max_abs_difference_sats"))],
        ["classified inputs and outputs", agreement.get("classified")],
        ["classification agreement", metric_value(agreement.get("agreement_rate"))],
    ]
    if with_blocksci:
        summary_rows.extend(
            [
                ["BlockSci max interim difference", sats(blocksci.get("interim_max_abs_difference_sats"))],
                ["BlockSci classification agreement", metric_value(blocksci_agreement.get("agreement_rate"))],
                ["labelled coinjoins BlockSci did not classify", blocksci.get("unclassified_coinjoins")],
                ["BlockSci coinjoins without a label", blocksci.get("extra_coinjoins")],
            ]
        )
    summary_rows.append(["dominant wallet", liquidity.get("dominant_wallet")])
    lines.extend(["", *key_values(summary_rows)])
    if blocksci and not with_blocksci:
        lines.extend(["", f"BlockSci liquidity classification unavailable: {blocksci.get('reason')}"])
    for title, analyzer, confusion in (
        ("Classification Disagreements", "observed", agreement.get("confusion")),
        ("BlockSci Classification Disagreements", "BlockSci", blocksci_agreement.get("confusion")),
    ):
        disagreements = [[key, count] for key, count in (confusion or {}).items() if is_disagreement(key)]
        if disagreements:
            lines.extend(section(title, *table([f"ground truth -> {analyzer}", "count"], disagreements), level=3))

    large_wallets = liquidity.get("large_wallets") or []
    if large_wallets:
        rows = []
        for wallet in large_wallets:
            fin = wallet.get("fin") or {}
            rows.append(
                [
                    wallet.get("wallet"),
                    metric_value(wallet.get("peak_pool_share")),
                    wallet.get("first_enter_index"),
                    fin.get("observed_entry_index"),
                    wallet.get("last_leave_index"),
                    fin.get("descending_window"),
                    metric_value(fin.get("window_leave_precision")),
                    metric_value(fin.get("lift")),
                ]
            )
        headers = [
            "wallet",
            "peak pool share",
            "first entry",
            "observed entry",
            "last leave",
            "descending window",
            "leave precision",
            "lift",
        ]
        lines.extend(section("Large Wallets", *table(headers, rows), level=3))

    large_groups = liquidity.get("large_groups") or []
    if large_groups:
        rows = []
        for group in large_groups:
            fin = group.get("fin") or {}
            rows.append(
                [
                    group.get("group"),
                    ", ".join(group.get("wallets") or []),
                    metric_value(group.get("peak_pool_share")),
                    group.get("first_enter_index"),
                    fin.get("observed_entry_index"),
                    group.get("last_leave_index"),
                    fin.get("descending_window"),
                    metric_value(fin.get("window_leave_precision")),
                    metric_value(fin.get("window_leave_recall")),
                    metric_value(fin.get("lift")),
                ]
            )
        headers = [
            "group",
            "wallets",
            "peak pool share",
            "first entry",
            "observed entry",
            "last leave",
            "descending window",
            "leave precision",
            "leave recall",
            "lift",
        ]
        lines.extend(section("Wallet Groups", *table(headers, rows), level=3))

    spends = liquidity.get("postmix_spends")
    if spends:
        lines.extend(
            [
                "",
                f"Scheduled postmix spends: {spends.get('scheduled')} "
                f"({', '.join(f'{status} {count}' for status, count in (spends.get('by_status') or {}).items())}); "
                f"{spends.get('confirmed_postmix')} confirmed as spends of coinjoin outputs.",
            ]
        )
    lines.extend(["", "Per-wallet events: " + ", ".join(f"`{name}`" for name in liquidity.get("dataset_files") or [])])
    return lines


def render_skipped_transactions(report: JsonObject, explorer_base_url: str) -> list[str]:
    skipped_txids = report.get("blocksci_skipped_txids") or []
    if not skipped_txids:
        return []

    return section(
        "BlockSci Skipped Transactions",
        "These transactions hit the JoinMarket detector timeout or max-depth limit and are not counted as "
        "ordinary false negatives.",
        "",
        *table(["txid"], [[tx_value(txid, explorer_base_url)] for txid in skipped_txids]),
    )


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
        for item in io_items(source)
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
                tx_value(record.get("prev_txid"), explorer_base_url, short=True),
                record.get("spending_tx"),
                record.get("spend_by_tx"),
            ]
        )

    headers = [
        "index",
        "value sats",
        "address",
        "script type",
        "BlockSci type",
        "wallet",
        "prev txid",
        "spending tx",
        "spend by tx",
    ]
    return [f"#### {title}", "", *table(headers, rows), ""]


IMAGE_LABELS = (
    ("blocksci", "BlockSci"),
    ("coinjoin_analysis", "coinjoin-analysis"),
    ("coinjoin_emulator", "coinjoin-emulator"),
    ("uploader", "uploader"),
    ("unified_report", "unified-report"),
    ("mappings_enumerator", "mappings-enumerator"),
    ("sake", "Sake"),
)
OPTIONAL_IMAGES = {"mappings_enumerator", "sake"}


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
    trees = manifest.get("source_trees") or {}
    comparison = report.get("run_manifest_comparison") or {}

    image_rows = [
        row
        for key, label in IMAGE_LABELS
        # The mapping-stage images exist only in reports with a mapping stage.
        if key in images or key not in OPTIONAL_IMAGES
        for row in ([f"{label} image", images.get(key)], [f"{label} image digest", image_digests.get(key)])
    ]
    lines = section(
        "Run Manifest",
        *key_values(
            [
                ["scenario sha256", scenario.get("sha256")],
                ["engine", execution.get("engine")],
                ["coinjoin type", execution.get("coinjoin_type")],
                ["detector", json.dumps(detector, sort_keys=True)],
                *image_rows,
                ["coinjoin-emulator commit", commits.get("coinjoin_emulator")],
                ["exporters commit", commits.get("exporters")],
                ["exporters tree sha256", trees.get("exporters_sha256")],
            ],
            key="field",
        ),
    )

    if comparison.get("available"):
        differences = comparison.get("differences") or []
        lines.extend(section("Run Manifest Comparison"))
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
        return section(
            "Inner Report Integration",
            "Inner report integration is NOT OK.",
            "",
            "No integration diagnostics are available in this report.",
        )

    images = diagnostics.get("images") or {}
    chain = diagnostics.get("chain") or {}
    target_txids = diagnostics.get("target_txids") or {}
    detector = diagnostics.get("detector") or {}
    complete_images = sum(1 for item in images.values() if (item or {}).get("status") == "ok")
    image_status = "ok" if images and complete_images == len(images) else "not_ok"
    problems = diagnostics.get("problems") or []
    verdict = (
        "Inner report integration is OK."
        if diagnostics.get("status") == "ok"
        else "Inner report integration is NOT OK."
    )
    lines = section(
        "Inner Report Integration",
        verdict,
        "",
        *table(
            ["check", "status", "details"],
            [
                [
                    "image provenance",
                    status_text(image_status),
                    f"{complete_images}/{len(images)} images complete",
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
    )
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
    lines = section(
        "CoinJoin Mapping Analysis",
        *key_values(
            [
                ["stage status", mappings.get("status")],
                ["enumerated transactions", enum_summary.get("transactions")],
                ["completed", enum_summary.get("completed")],
                ["timed out", enum_summary.get("timed_out")],
                ["errors", enum_summary.get("errors")],
                ["Sake seed", sake.get("seed")],
                ["Sake output match rate", metric_value(sake_summary.get("output_match_rate"))],
                ["Sake wallet match rate", metric_value(sake_summary.get("wallet_match_rate"))],
                ["Sake length match rate", metric_value(sake_summary.get("length_match_rate"))],
                ["Sake full CoinJoin match rate", metric_value(sake_summary.get("full_coinjoin_match_rate"))],
            ]
        ),
    )
    lines.extend(section("Per-transaction mapping results", level=3))
    rows = []
    sake_transactions = sake.get("transactions") or {}
    for txid, item in sorted((enumerator.get("transactions") or {}).items()):
        sake_item = sake_transactions.get(txid) or {}
        rows.append(
            [
                txid,
                item.get("status"),
                item.get("mapping_count"),
                item.get("retried"),
                sake_item.get("matched_outputs"),
                sake_item.get("total_outputs"),
                sake_item.get("full_coinjoin_match"),
            ]
        )
    lines.extend(
        table(
            ["txid", "status", "mappings", "retried", "Sake matched outputs", "Sake total outputs", "Sake full match"],
            rows,
        )
        if rows
        else ["No transactions."]
    )
    return lines


def render_header(report: JsonObject) -> list[str]:
    external = (report.get("run") or {}).get("mode") == "external"
    lines = [
        "# BlockSci CoinJoin Comparison Report" if external else "# BlockSci vs Emulator CoinJoin Report",
        "",
        "> `_N` means the block height was inferred from exported `block_*.json` files, not present in original "
        "`coinjoin_tx_info.json`.",
    ]
    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(section("Warnings"))
        lines.extend(f"> **{warning.get('code')}:** {warning.get('message')}" for warning in warnings)
    return lines


def render_run(report: JsonObject) -> list[str]:
    run = report.get("run") or {}
    scenario = report.get("scenario") or {}
    return section(
        "Run",
        *key_values(
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
                ["FirstWasabi2Block", run.get("first_wasabi2_block")],
                ["JoinMarket detector", run.get("joinmarket_detector")],
                ["JoinMarket min base fee", run.get("joinmarket_min_base_fee")],
                ["JoinMarket percentage fee", run.get("joinmarket_percentage_fee")],
                ["JoinMarket max depth", run.get("joinmarket_max_depth")],
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
                    ["BlockSci JoinMarket skipped", summary.get("blocksci_joinmarket_skipped")],
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


def render_scope_note(report: JsonObject) -> list[str]:
    """Why precision, recall and F1 are missing, when they are."""
    scope = report.get("evaluation_scope")
    if scope == "baseline_agreement_only":
        return [
            "",
            "> This report compares BlockSci with `coinjoin-analysis`. It has no emulator ground truth; "
            "precision, recall, and F1 are intentionally unavailable.",
        ]
    if scope != "emulator_labels_unavailable":
        return []
    lines = [
        "",
        "> Independent emulator producer labels were unavailable. Transaction labels remain unknown; "
        "precision, recall, and F1 are intentionally unavailable.",
    ]
    label_provenance = (report.get("emulator_data") or {}).get("label_provenance") or {}
    unavailable_reason = label_provenance.get("unavailable_reason")
    if unavailable_reason:
        lines.append(f"> Reason: {unavailable_reason}")
    return lines


def render_coverage(report: JsonObject) -> list[str]:
    coverage = report.get("coverage")
    if not coverage:
        return []
    return section(
        "Coverage",
        "> Only heights BlockSci scanned are compared; baseline transactions elsewhere are not BlockSci misses.",
        "",
        *key_values(
            [
                ["scan window", f"[{coverage.get('start_height')}, {coverage.get('end_height')})"],
                ["index blocks", coverage.get("index_blocks")],
                ["pinned at height", coverage.get("pin_height")],
                ["Dumplings snapshot height", coverage.get("dumplings_last_processed_height")],
                ["Dumplings commit", coverage.get("dumplings_commit")],
                ["baseline transactions", coverage.get("baseline_total")],
                ["baseline inside the window", coverage.get("baseline_in_window")],
                ["baseline outside the window", coverage.get("baseline_outside_window")],
                ["baseline not in the index or above the pin", coverage.get("baseline_not_in_index")],
            ],
            key="field",
        ),
    )


def render_baseline_filter(report: JsonObject) -> list[str]:
    baseline_filter = report.get("baseline_filter") or {}
    if not baseline_filter.get("enabled"):
        return []
    source_names = ", ".join(str(source.get("file")) for source in baseline_filter.get("sources", []))
    return section(
        "Baseline False-Positive Filter",
        *key_values(
            [
                ["source files", source_names],
                ["listed unique TXIDs", baseline_filter.get("listed_txids")],
                ["filtered baseline TXIDs", baseline_filter.get("filtered_count")],
            ],
            key="field",
        ),
    )


DIVERGENCE_KINDS = (
    ("missed_by_blocksci", "missed_by_blocksci"),
    ("blocksci_only", "blocksci_only"),
    ("shared_tx_mismatch", "shared_tx_mismatches"),
)


def render_analyzer_diagnostic(report: JsonObject, explorer_base_url: str) -> list[str]:
    """The analyzer-vs-analyzer comparison, transaction by transaction."""
    divergences = report["divergences"]
    lines = section(
        "CoinJoin-Analysis vs BlockSci Diagnostic",
        "This section preserves the analyzer comparison. Emulator reports use `emulator_data` as the "
        "canonical source for chain-wide detection metrics; external reports measure baseline agreement only.",
    )
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
        *render_integration_diagnostics(report),
        *render_run(report),
        *render_detection_summary(report),
        *render_scope_note(report),
        *render_coverage(report),
        *render_baseline_filter(report),
        *render_run_manifest(report),
        *render_coinjoin_mappings(report),
        *render_emulator_detection(report, explorer_base_url),
        *render_skipped_transactions(report, explorer_base_url),
        *render_clustering_evaluation(report),
        *render_liquidity_evaluation(report),
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
