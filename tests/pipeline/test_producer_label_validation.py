import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))

from exporters.comparison import build_detection_confusion_matrix
from exporters.emulator_data import load_producer_labels, verified_producer_label_sources


@pytest.fixture
def producer_run(tmp_path):
    data = tmp_path / "data"
    log = data / "wasabi-coordinator" / "Logs.txt"
    log.parent.mkdir(parents=True)
    txid = "a" * 64
    log.write_text(f"Round (round-1): Successfully broadcast the coinjoin: {txid}.\n", encoding="utf-8")
    source = {
        "path": "wasabi-coordinator/Logs.txt",
        "size_bytes": log.stat().st_size,
        "sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
    }
    manifest = {
        "schema_version": "1.0",
        "engine": "wasabi",
        "complete": True,
        "positive_count": 1,
        "sources": [source],
    }
    path = data / "coinjoin_label_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, path, log, manifest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "2.0"),
        ("engine", "joinmarket"),
        ("complete", False),
        ("positive_count", True),
        ("positive_count", -1),
        ("positive_count", 1.5),
        ("sources", []),
    ],
)
def test_invalid_producer_manifest_disables_ground_truth(producer_run, field, value):
    run, path, _log, manifest = producer_run
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sources, provenance = verified_producer_label_sources(run, "wasabi2")
    assert sources == []
    assert provenance["independent"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("size_bytes", True), ("size_bytes", -1), ("sha256", "invalid"), ("sha256", "0" * 64)],
)
def test_invalid_source_metadata_disables_ground_truth(producer_run, field, value):
    run, path, _log, manifest = producer_run
    if field == "size_bytes" and value is True:
        _log.write_bytes(b"x")
        manifest["sources"][0]["sha256"] = hashlib.sha256(b"x").hexdigest()
    manifest["sources"][0][field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sources, provenance = verified_producer_label_sources(run, "wasabi2")
    assert sources == []
    assert provenance["independent"] is False


def test_duplicate_source_disables_ground_truth(producer_run):
    run, path, _log, manifest = producer_run
    manifest["sources"].append(dict(manifest["sources"][0]))
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sources, provenance = verified_producer_label_sources(run, "wasabi2")
    assert sources == []
    assert "duplicate source" in provenance["unavailable_reason"]


@pytest.mark.parametrize("relative_path", ["", "../outside/Logs.txt", "missing/Logs.txt"])
def test_invalid_source_path_disables_ground_truth(producer_run, relative_path):
    run, path, _log, manifest = producer_run
    manifest["sources"][0]["path"] = relative_path
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sources, provenance = verified_producer_label_sources(run, "wasabi2")
    assert sources == []
    assert provenance["independent"] is False


def test_same_size_source_edit_disables_ground_truth(producer_run):
    run, _path, log, _manifest = producer_run
    original = log.read_bytes()
    log.write_bytes(b"modified" + original[8:])
    assert log.stat().st_size == len(original)
    sources, provenance = verified_producer_label_sources(run, "wasabi2")
    assert sources == []
    assert "hash does not match" in provenance["unavailable_reason"]


def test_producer_count_must_match_source(producer_run):
    run, path, _log, manifest = producer_run
    manifest["positive_count"] = 2
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sources, provenance = verified_producer_label_sources(run, "wasabi2")
    load_producer_labels(run, "wasabi2", sources, provenance)
    assert provenance["independent"] is False
    assert "does not match manifest" in provenance["unavailable_reason"]


@pytest.mark.parametrize("status", ["multiple_matches", "duplicate_destination"])
def test_joinmarket_destination_conflicts_disable_ground_truth(tmp_path, status):
    data = tmp_path / "data"
    data.mkdir()
    events = data / "joinmarket_round_events.json"
    events.write_text(
        json.dumps(
            [
                {
                    "export_round_id": "round-1",
                    "status": status,
                    "destination_matches": [{"txid": "a" * 64}],
                }
            ]
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "1.0",
        "engine": "joinmarket",
        "complete": True,
        "positive_count": 1,
        "sources": [
            {
                "path": events.name,
                "size_bytes": events.stat().st_size,
                "sha256": hashlib.sha256(events.read_bytes()).hexdigest(),
            }
        ],
    }
    (data / "coinjoin_label_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sources, provenance = verified_producer_label_sources(tmp_path, "joinmarket")
    load_producer_labels(tmp_path, "joinmarket", sources, provenance)
    assert provenance["independent"] is False
    assert "destination conflict" in provenance["unavailable_reason"]


def test_baseline_omission_is_a_reported_false_negative():
    ground_truth = {"label_provenance": {"independent": True}, "transactions": {"txA": {"is_coinjoin": True}}}
    metrics = build_detection_confusion_matrix(ground_truth, {})
    assert metrics["false_negatives"] == 1
    assert metrics["false_negative_txids"] == ["txA"]
