"""The record helpers the report sections share."""

from exporters.common import io_items, wallet_names


def test_shared_record_helpers():
    record = {
        "inputs": [{"wallet_name": "wallet-001"}, {"wallet_name": None}],
        "outputs": [{"wallet_name": "wallet-000"}, {"wallet_name": "wallet-001"}],
    }
    assert len(list(io_items(record))) == 4
    assert wallet_names(record) == ["wallet-000", "wallet-001"]
    assert wallet_names(None) == []


def test_run_start_is_read_from_both_run_id_forms():
    from exporters.common import parse_run_started_at

    assert parse_run_started_at("2026-11-12_17-06-42_overactive-local") == "2026-11-12T17:06:42"
    assert parse_run_started_at("2026-05-24_15-42_default") == "2026-05-24T15:42:00"
    assert parse_run_started_at("2026-05-24_15-42_10-wallets") == "2026-05-24T15:42:00"
    assert parse_run_started_at("mainnet-index") is None
