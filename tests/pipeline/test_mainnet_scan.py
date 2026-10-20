"""The mainnet scan aggregates over every detection and says when its result is incomplete."""

import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "metacentrum_mainnet_coinjoin_scan.py"


class FakeValue:
    def __init__(self, value):
        self.value = value


class FakeTx:
    def __init__(self, index, when):
        self.hash = f"{index:064x}"
        self.block_height = 700_000 + index
        self.block_time = when
        self.input_count = 3
        self.output_count = 2
        self.inputs = [FakeValue(10)] * 3
        self.outputs = [FakeValue(14)] * 2


class FakeBlock:
    hash = "00" * 32


class FakeChain:
    opened = []

    def __init__(self, config, max_block=None):
        FakeChain.opened.append((config, max_block))
        self.blocks = 800_000 if max_block is None else max_block
        self.calls = []

    def __len__(self):
        return self.blocks

    def __getitem__(self, height):
        return FakeBlock()

    def filter_coinjoin_txes(self, start, end, kind, *minimum):
        self.calls.append((start, end, kind, *minimum))
        first = datetime(2024, 1, 31, 12)
        return [FakeTx(index, first + timedelta(days=index)) for index in range(5)]

    def scan_coinjoins_by_subset_matching(self, start, end, *settings):
        return self.filter_coinjoin_txes(start, end, "joinmarket")[:2], [FakeTx(9, None)]


@pytest.fixture
def scanner(monkeypatch):
    monkeypatch.setitem(sys.modules, "blocksci", types.SimpleNamespace(Blockchain=FakeChain))
    for name in (
        "BLOCKSCI_CONFIG",
        "BLOCKSCI_OUTPUT_DIR",
        "BLOCKSCI_MAX_BLOCK",
        "SCAN_START_HEIGHT",
        "SCAN_END_HEIGHT",
    ):
        monkeypatch.delenv(name, raising=False)
    FakeChain.opened = []
    spec = importlib.util.spec_from_file_location("mainnet_scan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(scanner, tmp_path, *flags):
    output = tmp_path / "summary.json"
    status = scanner.main(["--config", "config.json", "--output", str(output), *flags])
    return status, json.loads(output.read_text())


def test_aggregates_cover_every_detection_not_only_the_preview(scanner, tmp_path):
    status, report = run(scanner, tmp_path, "--coinjoin-type", "wasabi2", "--max-records", "2")

    assert status == 0
    assert report["status"] == "complete"
    assert report["detected_count"] == 5
    assert len(report["records_preview"]) == 2
    assert report["records_preview_truncated"] is True
    assert report["monthly"] == {"2024-01": 1, "2024-02": 4}
    assert sum(report["daily"].values()) == 5
    lines = (tmp_path / report["records_file"]).read_text().splitlines()
    assert len(lines) == 5


def test_pinned_index_reports_its_last_height_not_a_block_count(scanner, tmp_path):
    status, report = run(scanner, tmp_path, "--coinjoin-type", "wasabi2", "--max-block", "850000")

    assert status == 0
    assert FakeChain.opened == [("config.json", 850000)]
    assert report["index"]["blocks"] == 850000
    assert report["index"]["last_height"] == 849999
    assert report["scan_range"] == {"start_height": 0, "end_height": 850000, "end_exclusive": True}


def test_range_and_threshold_reach_the_detector(scanner, tmp_path, monkeypatch):
    calls = []
    original = FakeChain.filter_coinjoin_txes

    def recording(self, *args):
        calls.append(args)
        return original(self, *args)

    monkeypatch.setattr(FakeChain, "filter_coinjoin_txes", recording)
    monkeypatch.setenv("SCAN_START_HEIGHT", "741213")
    monkeypatch.setenv("SCAN_END_HEIGHT", "750000")

    status, _ = run(scanner, tmp_path, "--coinjoin-type", "wasabi2", "--min-input-count", "15")

    assert status == 0
    assert calls == [(741213, 750000, "wasabi2", 15)]


def test_skipped_joinmarket_candidates_make_the_scan_partial(scanner, tmp_path):
    status, report = run(scanner, tmp_path, "--coinjoin-type", "joinmarket")

    assert status == 0
    assert report["status"] == "partial"
    assert report["joinmarket"]["skipped_count"] == 1
    assert report["detected_count"] == 2


def test_a_range_past_the_index_fails_with_a_summary(scanner, tmp_path):
    status, report = run(scanner, tmp_path, "--coinjoin-type", "wasabi2", "--max-block", "100", "--end-height", "200")

    assert status == 1
    assert report["status"] == "failed"
    assert "outside the chain of 100 blocks" in report["error"]
