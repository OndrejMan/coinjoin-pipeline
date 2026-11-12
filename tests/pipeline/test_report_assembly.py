"""The record helpers the report sections share."""

import json
from pathlib import Path

from exporters.artifact_paths import EXPORTED_BLOCKS_DIR
from exporters.common import f1_score, io_items, wallet_names
from exporters.normalization import iter_exported_blocks


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


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


def test_run_start_is_read_from_both_run_id_forms():
    from exporters.common import parse_run_started_at

    assert parse_run_started_at("2026-11-12_17-06-42_overactive-local") == "2026-11-12T17:06:42"
    assert parse_run_started_at("2026-05-24_15-42_default") == "2026-05-24T15:42:00"
    assert parse_run_started_at("2026-05-24_15-42_10-wallets") == "2026-05-24T15:42:00"
    assert parse_run_started_at("mainnet-index") is None


def test_the_manifest_identifies_the_exporters_by_their_tree_alone(tmp_path):
    from exporters.manifest import build_run_manifest
    from exporters.parameters import Detector as ReportDetector

    manifest = build_run_manifest(tmp_path / "run-a", None, ReportDetector("wasabi2"), emulator_git_commit="abc123")
    # The report container has no .git to read an exporters commit or dirty flag from.
    assert manifest["source_commits"] == {"coinjoin_emulator": "abc123"}
    assert list(manifest["source_trees"]) == ["exporters_sha256"]
