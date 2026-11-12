"""Report assembly through the exporter CLI and the helpers its sections share."""

import json
from pathlib import Path

from exporters import cli as report_cli
from exporters.artifact_paths import EXPORTED_BLOCKS_DIR
from exporters.common import f1_score, io_items, wallet_names
from exporters.analysis_artifact import SCHEMA_VERSION
from exporters.parameters import Detector
from exporters.normalization import iter_exported_blocks

TXID = "a" * 64


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def stored_run(tmp_path: Path) -> Path:
    run = tmp_path / "run-a"
    write(run / "coinjoin-analysis_data/coinjoin_tx_info.json", {"coinjoins": {TXID: {"txid": TXID}, "b" * 64: {}}})
    args = report_cli.parse_args(["--run-dir", str(run)])
    write(
        run / "blocksci-analysis_data/blocksci_analysis.json",
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run.name,
            "parameters": Detector.from_args(args).artifact_parameters(),
            "first_wasabi2_block": 0,
            "records": {},
            "skipped_txids": [],
            "integration_diagnostics": {},
        },
    )
    return run


def test_report_replaces_a_previous_report_that_is_not_an_object(tmp_path):
    run = stored_run(tmp_path)
    write(run / "coinjoinPipeline_data/unified_report.json", ["not", "a", "report"])

    assert report_cli.main(["--run-dir", str(run)]) == 0

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
