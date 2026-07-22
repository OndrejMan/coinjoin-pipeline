import argparse
import json
from pathlib import Path
import sys
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))

from exporters.blocksci_analysis import (  # noqa: E402
    SCHEMA_VERSION,
    detector_parameters,
    exported_addresses,
    load_analysis,
    write_analysis,
)
from exporters import cli as report_cli  # noqa: E402


def parameters() -> dict:
    return detector_parameters(
        argparse.Namespace(
            coinjoin_type="wasabi2",
            min_input_count=None,
            test_values=True,
            joinmarket_detector="definite",
            joinmarket_min_base_fee=5000,
            joinmarket_percentage_fee=0.00004,
            joinmarket_max_depth=200000,
        )
    )


def test_exported_addresses_reads_all_exported_block_outputs(tmp_path: Path) -> None:
    block_dir = tmp_path / "coinjoin_emulator_data" / "data" / "btc-node"
    block_dir.mkdir(parents=True)
    (block_dir / "block_1.json").write_text(
        json.dumps(
            {
                "tx": [
                    {
                        "vout": [
                            {"scriptPubKey": {"address": "bcrt1-one"}},
                            {"scriptPubKey": {"addresses": ["bcrt1-two"]}},
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert exported_addresses(tmp_path) == {"bcrt1-one", "bcrt1-two"}


def test_load_analysis_rejects_parameter_mismatch(tmp_path: Path) -> None:
    artifact = tmp_path / "blocksci_analysis.json"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": "run-1",
                "parameters": parameters(),
                "first_wasabi2_block": 0,
                "records": {},
                "skipped_txids": [],
                "integration_diagnostics": {"status": "ok"},
                "predicted_address_clusters": {},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_analysis(
        artifact, run_id="run-1", expected_parameters=parameters()
    )
    assert loaded["records"] == {}

    mismatched = {**parameters(), "test_values": False}
    with pytest.raises(ValueError, match="parameters do not match"):
        load_analysis(
            artifact,
            run_id="run-1",
            expected_parameters=mismatched,
        )


def test_write_analysis_persists_all_heavy_results(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    config = run_dir / "blocksci_data" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    args = argparse.Namespace(
        run_dir=run_dir,
        config=config,
        coinjoin_type="wasabi2",
        min_input_count=None,
        test_values=True,
        joinmarket_detector="definite",
        joinmarket_min_base_fee=5000,
        joinmarket_percentage_fee=0.00004,
        joinmarket_max_depth=200000,
        blocksci_image="blocksci:test",
        coinjoin_analysis_image="analysis:test",
        coinjoin_emulator_image="emulator:test",
        wrapper_image="pipeline:test",
    )
    fake_blocksci = mock.Mock()
    with (
        mock.patch("exporters.blocksci_analysis.blocksci", fake_blocksci),
        mock.patch(
            "exporters.blocksci_analysis.export_blocksci_records",
            return_value=({"tx": {"txid": "tx"}}, ["skipped"]),
        ),
        mock.patch(
            "exporters.blocksci_analysis.build_integration_diagnostics",
            return_value={"status": "ok"},
        ),
        mock.patch(
            "exporters.blocksci_analysis.exported_addresses",
            return_value={"bcrt1-address"},
        ),
        mock.patch(
            "exporters.blocksci_analysis.export_blocksci_cluster_assignments_for_addresses",
            return_value=({"bcrt1-address": "7"}, None),
        ),
        mock.patch(
            "exporters.blocksci_analysis.load_first_wasabi2_block",
            return_value=850237,
        ),
    ):
        output = write_analysis(args)

    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["records"] == {"tx": {"txid": "tx"}}
    assert artifact["predicted_address_clusters"] == {"bcrt1-address": "7"}
    assert artifact["integration_diagnostics"] == {"status": "ok"}
    fake_blocksci.heuristics.set_test_values_enabled.assert_called_once_with(True)


def test_report_cli_consumes_artifact_without_blocksci(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    baseline_dir = run_dir / "coinjoin-analysis_data"
    baseline_dir.mkdir(parents=True)
    (baseline_dir / "coinjoin_tx_info.json").write_text(
        '{"coinjoins": {}}', encoding="utf-8"
    )
    emulator_dir = run_dir / "coinjoin_emulator_data"
    emulator_dir.mkdir()
    (emulator_dir / "scenario.json").write_text('{"name": "fixture"}', encoding="utf-8")
    artifact = run_dir / "blocksci-analysis_data" / "blocksci_analysis.json"
    artifact.parent.mkdir()
    artifact.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": "run-1",
                "parameters": parameters(),
                "first_wasabi2_block": 0,
                "records": {},
                "skipped_txids": [],
                "integration_diagnostics": {"status": "ok"},
                "predicted_address_clusters": {},
                "cluster_export_error": None,
            }
        ),
        encoding="utf-8",
    )

    with (
        mock.patch.object(report_cli, "blocksci", None),
        mock.patch.object(
            report_cli,
            "export_blocksci_records",
            side_effect=AssertionError("BlockSci must not be queried"),
        ),
        mock.patch.object(report_cli, "build_emulator_data", return_value=None),
        mock.patch.object(report_cli, "build_report", return_value={"schema_version": "test"}),
    ):
        code = report_cli.main(
            [
                "--runs-root",
                str(tmp_path),
                "--run-dir",
                "run-1",
                "--blocksci-analysis",
                str(artifact),
                "--coinjoin-type",
                "wasabi2",
                "--test-values",
            ]
        )

    assert code == 0
    assert (run_dir / "coinjoinPipeline_data" / "unified_report.json").is_file()
