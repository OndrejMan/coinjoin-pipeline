"""Transaction-centric pool liquidity: ground truth, observed classification, dataset."""

import csv
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from exporters.artifact_paths import POSTMIX_SPENDS_FILE  # noqa: E402
from exporters.liquidity import build_liquidity, save_liquidity_dataset  # noqa: E402
from exporters.markdown_report import render_liquidity_evaluation  # noqa: E402

BIG, SMALL, OTHER = "wallet-000", "wallet-001", "wallet-002"
FUND, CJ1, CJ2, CJ3, P1, P2 = "fund", "cj1", "cj2", "cj3", "postmix1", "postmix2"


def io(index, value, wallet, prev=None):
    record = {"index": str(index), "value": value, "address": f"{wallet}-{value}", "wallet_name": wallet}
    if prev:
        record.update({"prev_txid": prev[0], "prev_vout": str(prev[1])})
    return record


def tx(txid, height, coinjoin, inputs, outputs):
    return {"txid": txid, "block_height": height, "is_coinjoin": coinjoin, "inputs": inputs, "outputs": outputs}


def emulator_data():
    """A large wallet enters, remixes and spends out; a small one keeps mixing; one stays put."""
    transactions = [
        tx(FUND, 100, False, [], [io(0, 1000, BIG), io(1, 100, SMALL), io(2, 100, SMALL), io(3, 300, OTHER)]),
        tx(
            CJ1,
            101,
            True,
            [io(0, 1000, BIG, (FUND, 0)), io(1, 100, SMALL, (FUND, 1)), io(2, 300, OTHER, (FUND, 3))],
            [
                io(0, 500, BIG),
                io(1, 490, BIG),
                io(2, 50, SMALL),
                io(3, 50, SMALL),
                io(4, 150, OTHER),
                io(5, 150, OTHER),
            ],
        ),
        tx(
            CJ2,
            102,
            True,
            [io(0, 500, BIG, (CJ1, 0)), io(1, 50, SMALL, (CJ1, 2)), io(2, 100, SMALL, (FUND, 2))],
            [io(0, 495, BIG), io(1, 50, SMALL), io(2, 98, SMALL)],
        ),
        tx(
            CJ3,
            103,
            True,
            [io(0, 50, SMALL, (CJ2, 1)), io(1, 98, SMALL, (CJ2, 2))],
            [io(0, 74, SMALL), io(1, 73, SMALL)],
        ),
        tx(P1, 104, False, [io(0, 490, BIG, (CJ1, 1)), io(1, 495, BIG, (CJ2, 0))], [io(0, 980, None)]),
        tx(P2, 105, False, [io(0, 74, SMALL, (CJ3, 0))], [io(0, 70, None)]),
    ]
    return {
        "coinjoin_type": "wasabi2",
        "label_provenance": {"independent": True},
        "transactions": {record["txid"]: record for record in transactions},
    }


def observed(events):
    return {
        side: {str(index): {"mix_event_type": event} for index, event in enumerate(items)}
        for side, items in events.items()
    }


def coinjoin_analysis_data():
    """coinjoin-analysis misses the two postmix spends, as before the spend-reference fix."""
    return {
        "coinjoins": {
            CJ1: observed(
                {
                    "inputs": ["MIX_ENTER"] * 3,
                    "outputs": ["MIX_REMIX", "MIX_STAY", "MIX_REMIX", "MIX_STAY", "MIX_STAY", "MIX_STAY"],
                }
            ),
            CJ2: observed(
                {"inputs": ["MIX_REMIX", "MIX_REMIX", "MIX_ENTER"], "outputs": ["MIX_LEAVE", "MIX_REMIX", "MIX_REMIX"]}
            ),
            CJ3: observed({"inputs": ["MIX_REMIX"] * 2, "outputs": ["MIX_STAY"] * 2}),
        }
    }


@pytest.fixture(name="run_dir")
def fixture_run_dir(tmp_path):
    run_dir = tmp_path / "2026-10-10_12-00_liquidity"
    spends = run_dir / POSTMIX_SPENDS_FILE
    spends.parent.mkdir(parents=True)
    spends.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "spends": [
                    {
                        "wallet": "wasabi-client-000",
                        "wallet_index": 0,
                        "status": "sent",
                        "txid": P1,
                        "amount_sats": 985,
                    },
                    {"wallet": "wasabi-client-000", "wallet_index": 0, "status": "not_due", "delay_rounds": 99},
                ],
            }
        )
    )
    return run_dir


def build(run_dir):
    return build_liquidity(run_dir, emulator_data(), coinjoin_analysis_data())


def events(dataset, txid, side):
    coinjoin = next(item for item in dataset["coinjoins"] if item["txid"] == txid)
    return [item["event"] for item in coinjoin[side]]


def test_ground_truth_follows_the_spending_transaction(run_dir):
    _, dataset = build(run_dir)

    assert [item["txid"] for item in dataset["coinjoins"]] == [CJ1, CJ2, CJ3]
    assert events(dataset, CJ1, "inputs") == ["MIX_ENTER"] * 3
    assert events(dataset, CJ1, "outputs") == [
        "MIX_REMIX",
        "MIX_LEAVE",
        "MIX_REMIX",
        "MIX_STAY",
        "MIX_STAY",
        "MIX_STAY",
    ]
    assert events(dataset, CJ2, "inputs") == ["MIX_REMIX", "MIX_REMIX", "MIX_ENTER"]
    assert events(dataset, CJ3, "outputs") == ["MIX_LEAVE", "MIX_STAY"]
    leaving = dataset["coinjoins"][0]["outputs"][1]
    assert (leaving["spent_by"], leaving["spend_distance_coinjoins"]) == (P1, 2)
    assert [item["is_standard_denom"] for item in dataset["coinjoins"][0]["outputs"]] == [
        False,
        False,
        True,
        True,
        True,
        True,
    ]


def test_pool_series_follow_the_paper_metrics(run_dir):
    _, dataset = build(run_dir)
    pool = dataset["pool"]["ground_truth"]

    assert pool["enter"] == [1400, 100, 0]
    assert pool["leave"] == [490, 495, 74]
    assert pool["interim"] == [910, 515, 441]
    assert pool["unmoved_window"] == [350, 350, 423]
    assert pool["active_remix"] == [560, 165, 18]
    assert dataset["pool"]["observed"]["interim"] == [1400, 1005, 1005]


def test_wallet_decomposition_flags_only_the_large_wallet(run_dir):
    section, dataset = build(run_dir)

    big = dataset["wallets"][BIG]
    assert big["series"]["interim"] == [510, 15, 15]
    assert (big["first_enter_index"], big["last_leave_index"], big["peak_pool_share"]) == (0, 1, 0.56044)
    assert dataset["wallets"][SMALL]["peak_pool_share"] == 0.38835
    assert section["dominant_wallet"] == BIG
    assert [wallet["wallet"] for wallet in section["large_wallets"]] == [BIG]


def test_fin_attributes_the_descending_leaves_to_the_large_wallet(run_dir):
    section, _ = build(run_dir)
    fin = section["large_wallets"][0]["fin"]

    assert fin["observed_entry_index"] == 0
    assert fin["entry_wallet_share"] == 0.714286
    assert fin["descending_window"] == [0, 1]
    assert fin["window_leave_precision"] == 1.0
    assert fin["baseline_leave_share"] == 0.930123
    assert fin["lift"] == 1.075127


def test_observed_classification_is_compared_per_input_and_output(run_dir):
    section, _ = build(run_dir)
    agreement = section["classification_agreement"]

    assert (agreement["classified"], agreement["agreeing"], agreement["agreement_rate"]) == (19, 17, 0.894737)
    assert agreement["confusion"]["outputs:MIX_LEAVE->MIX_STAY"] == 2
    assert section["interim_max_abs_difference_sats"] == 564


def test_scheduled_spends_are_checked_against_the_blocks(run_dir):
    section, dataset = build(run_dir)

    assert section["postmix_spends"] == {"scheduled": 2, "by_status": {"not_due": 1, "sent": 1}, "confirmed_postmix": 1}
    assert dataset["postmix_spends"][0]["wallet"] == BIG
    assert dataset["postmix_spends"][0]["spends_coinjoin_outputs"] is True


def test_dataset_is_written_as_json_and_one_csv_row_per_event(run_dir, tmp_path):
    _, dataset = build(run_dir)

    save_liquidity_dataset(tmp_path, dataset)

    assert json.loads((tmp_path / "liquidity_dataset.json").read_text())["schema_version"] == "1.0"
    with (tmp_path / "liquidity_events.csv").open(encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 19
    assert rows[4] == {
        "coinjoin_index": "0",
        "txid": CJ1,
        "block_height": "101",
        "side": "output",
        "io_index": "1",
        "wallet": BIG,
        "value_sats": "490",
        "event": "MIX_LEAVE",
        "observed_event": "MIX_STAY",
        "spent_by": P1,
        "spend_distance_coinjoins": "2",
        "is_standard_denom": "False",
    }


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (None, "not an emulator report"),
        ({"label_provenance": {"independent": False}, "transactions": {}}, "labels are unavailable"),
        ({"label_provenance": {"independent": True}, "transactions": {}}, "no labelled coinjoin"),
    ],
)
def test_unavailable_without_a_known_mix_boundary(tmp_path, data, reason):
    section, dataset = build_liquidity(tmp_path, data, {"coinjoins": {}})

    assert section["available"] is False
    assert reason in section["reason"]
    assert dataset is None


def test_markdown_lists_disagreements_and_the_large_wallet(run_dir):
    section, _ = build(run_dir)

    markdown = "\n".join(render_liquidity_evaluation({"liquidity_evaluation": section}))

    assert "## Liquidity Evaluation" in markdown
    assert "| outputs:MIX_LEAVE->MIX_STAY | 2 |" in markdown
    assert "| wallet-000 | 0.56044 | 0 | 0 | 1 | [0, 1] | 1 | 1.075127 |" in markdown
    assert "Scheduled postmix spends: 2 (not_due 1, sent 1); 1 confirmed" in markdown
