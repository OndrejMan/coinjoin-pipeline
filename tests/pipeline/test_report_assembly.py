"""Report assembly through the exporter CLI and the helpers its sections share."""

import json
from pathlib import Path

from exporters import cli as report_cli
from exporters.analysis_artifact import detector_parameters
from exporters.artifact_paths import DUMPLINGS_SNAPSHOT_FILE, EXPORTED_BLOCKS_DIR
from exporters.common import f1_score, io_items, wallet_names
from exporters.markdown_report import is_disagreement, render_run_manifest
from exporters.normalization import iter_exported_blocks

TXID = "a" * 64


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def external_run(tmp_path: Path) -> Path:
    run = tmp_path / "mainnet-compare"
    write(run / "coinjoin-analysis_data/coinjoin_tx_info.json", {"coinjoins": {TXID: {"txid": TXID}, "b" * 64: {}}})
    write(run / DUMPLINGS_SNAPSHOT_FILE, {"last_processed_height": 850100, "dumplings_commit": "abc123"})
    args = report_cli.parse_args(["--run-dir", str(run), "--mode", "external"])
    write(
        run / "blocksci-analysis_data/blocksci_analysis.json",
        {
            "schema_version": "1.1",
            "mode": "external",
            "run_id": run.name,
            "parameters": detector_parameters(args),
            "first_wasabi2_block": 0,
            "records": {},
            "skipped_txids": [],
            "integration_diagnostics": None,
            "scan_window": {"start_height": 850000, "end_height": 850050, "end_exclusive": True},
            "baseline_heights": {TXID: 850010, "b" * 64: None},
        },
    )
    return run


def test_external_report_records_window_snapshot_and_its_inputs(tmp_path):
    run = external_run(tmp_path)

    assert report_cli.main(["--run-dir", str(run), "--mode", "external", "--network", "bitcoin"]) == 0

    report = json.loads((run / "coinjoinPipeline_data/unified_report.json").read_text())
    coverage = report["coverage"]
    assert coverage["baseline_in_window"] == 1
    assert coverage["baseline_not_in_index"] == 1
    assert coverage["dumplings_last_processed_height"] == 850100
    assert coverage["dumplings_commit"] == "abc123"
    assert report["evaluation_scope"] == "baseline_agreement_only"
    assert report["baseline_filter"]["enabled"] is False
    # The snapshot shapes the coverage, so changing it must mark the report stale.
    assert DUMPLINGS_SNAPSHOT_FILE in report["run_manifest"]["inputs"]


def test_report_replaces_a_previous_report_that_is_not_an_object(tmp_path):
    run = external_run(tmp_path)
    write(run / "coinjoinPipeline_data/unified_report.json", ["not", "a", "report"])

    assert report_cli.main(["--run-dir", str(run), "--mode", "external"]) == 0

    report = json.loads((run / "coinjoinPipeline_data/unified_report.json").read_text())
    assert report["run_manifest_comparison"]["available"] is False


def test_exported_blocks_are_read_in_height_order_with_file_name_fallback(tmp_path):
    for height in (10, 9, 2):
        write(
            tmp_path / EXPORTED_BLOCKS_DIR / f"block_{height}.json", {"tx": []} if height == 9 else {"height": height}
        )

    assert [(path.name, height) for path, height, _block in iter_exported_blocks(tmp_path)] == [
        ("block_2.json", 2),
        ("block_9.json", 9),
        ("block_10.json", 10),
    ]
    assert list(iter_exported_blocks(tmp_path / "missing")) == []


def test_shared_record_helpers():
    record = {
        "inputs": [{"wallet_name": "wallet-001"}, {"wallet_name": None}],
        "outputs": [{"wallet_name": "wallet-000"}, {"wallet_name": "wallet-001"}],
    }
    assert len(list(io_items(record))) == 4
    assert wallet_names(record) == ["wallet-000", "wallet-001"]
    assert wallet_names(None) == []
    assert f1_score(0.5, 1.0) == 0.666667
    assert f1_score(0.0, 0.0) is None
    assert f1_score(None, 1.0) is None


def test_liquidity_disagreement_compares_the_two_events():
    assert is_disagreement("inputs:MIX_ENTER->MIX_REMIX")
    assert not is_disagreement("outputs:MIX_STAY->MIX_STAY")


def test_run_manifest_lists_mapping_images_only_when_recorded():
    manifest = {"images": {"blocksci": "blocksci:1"}, "image_digests": {}, "source_trees": {"exporters_sha256": "f0"}}
    plain = "\n".join(render_run_manifest({"run_manifest": manifest}))
    assert "Sake image" not in plain
    assert "| exporters tree sha256 | f0 |" in plain

    manifest["images"]["sake"] = "sake:1"
    manifest["images"]["mappings_enumerator"] = "enumerator:1"
    with_mappings = "\n".join(render_run_manifest({"run_manifest": manifest}))
    assert "| Sake image | sake:1 |" in with_mappings
    assert "| mappings-enumerator image | enumerator:1 |" in with_mappings
