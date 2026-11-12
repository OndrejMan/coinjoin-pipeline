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
