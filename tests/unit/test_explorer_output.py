"""The CoinJoin Explorer data the mainnet Dumplings baseline builds and uploads incrementally."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from coinjoin_pipeline.execution.pbs import explorer_output

MARCH = "2025-03-01 00-00-00--2025-04-01 00-00-00_unknown-static-100-1utxo"
APRIL = "2025-04-01 00-00-00--2025-05-01 00-00-00_unknown-static-100-1utxo"


def tx(time: str, enter: int = 0, leaves: tuple = (), relative_order: int = 0, outputs: tuple = ()) -> dict:
    coins = {str(i): {"value": v, "mix_event_type": "MIX_LEAVE", "burn_time": b} for i, (v, b) in enumerate(leaves)}
    for value in outputs:
        coins[str(len(coins))] = {"value": value, "mix_event_type": "MIX_REMIX"}
    return {
        "broadcast_time": time,
        "relative_order": relative_order,
        "block_index": 0,
        "inputs": {"0": {"value": enter, "mix_event_type": "MIX_ENTER"}} if enter else {},
        "outputs": coins,
    }


def write_month(coordinator: Path, month: str, coinjoins: dict) -> None:
    (coordinator / month).mkdir(parents=True, exist_ok=True)
    (coordinator / month / "coinjoin_tx_info.json").write_text(json.dumps({"coinjoins": coinjoins}))


@pytest.fixture
def scanner(tmp_path: Path) -> Path:
    root = tmp_path / "Scanner"
    pool = root / "wasabi2_opencoordinator"
    write_month(pool, MARCH, {
        "a": tx("2025-03-07 10:00:00.000", enter=500, leaves=((200, 7200.0),), relative_order=3),
        "b": tx("2025-03-07 11:00:00.000", outputs=(5, 5, 7)),
        "fp": tx("2025-03-07 11:30:00.000", enter=99),
    })
    write_month(pool, APRIL, {"c": tx("2025-04-01 00:00:00.000", enter=50)})
    (pool / "false_cjtxs.json").write_text(json.dumps({"manual": ["fp"]}))
    # relative_order is the remix depth: c entered fresh, so outside Wasabi 2 it must not sort first.
    whirlpool = root / "whirlpool_5M"
    write_month(whirlpool, MARCH, {
        "late": tx("2025-03-09 00:00:00.000", enter=1, relative_order=0),
        "early": tx("2025-03-08 00:00:00.000", enter=2, relative_order=4),
    })
    (root / "wasabi2").mkdir()
    (root / "wasabi2" / "coinjoin_tx_info.json").write_text(json.dumps({"coinjoins": {}}))
    (root / "Wasabi2CoinJoins.txt").write_text("raw\n")
    return root


def test_only_directories_with_monthly_intervals_are_coordinators(scanner: Path) -> None:
    assert explorer_output.coordinators(scanner) == ["wasabi2_opencoordinator", "whirlpool_5M"]


def test_curves_book_a_leaving_output_when_it_is_spent_and_skip_false_positives(scanner: Path) -> None:
    rows = explorer_output.coordinator_rows(scanner / "wasabi2_opencoordinator")
    assert [row["txid"] for row in rows] == ["a", "b", "c"]
    assert rows[1]["qt"] == 2 * 2 + 1
    curves = explorer_output.curves(rows)
    # Created: the output leaves at a, the coinjoin that made it.
    assert curves["created"] == [0, 300, 300, 350]
    # Spend: spent two hours after a, so it leaves at c, the first coinjoin after the spend.
    assert curves["spend"] == [0, 500, 500, 350]


def test_a_spend_after_the_last_coinjoin_stays_on_the_last_sample() -> None:
    rows = [explorer_output.compact("a", tx("2025-03-07 10:00:00.000", enter=10, leaves=((4, 1e9),)))]
    assert explorer_output.curves(rows)["spend"] == [0, 6]


def test_staging_numbers_non_wasabi2_coinjoins_chronologically(scanner: Path, tmp_path: Path) -> None:
    staging = tmp_path / "staged"
    explorer_output.stage_for_explorer(scanner, staging)
    month = json.loads((staging / "whirlpool_5M" / MARCH / "coinjoin_tx_info.json").read_text())["coinjoins"]
    assert month["early"]["relative_order"] < month["late"]["relative_order"]
    linked = staging / "wasabi2_opencoordinator" / MARCH / "coinjoin_tx_info.json"
    assert linked.is_symlink() and (staging / "wasabi2_opencoordinator" / "false_cjtxs.json").is_file()


def test_gzip_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "a.json"
    source.write_text('{"x": 1}')
    explorer_output.gzip_file(source, tmp_path / "one.gz")
    explorer_output.gzip_file(source, tmp_path / "two.gz")
    assert (tmp_path / "one.gz").read_bytes() == (tmp_path / "two.gz").read_bytes()
    assert gzip.decompress((tmp_path / "one.gz").read_bytes()) == b'{"x": 1}'


def test_output_holds_months_curves_and_baselines(scanner: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    explorer_output.build_output(scanner, out, 886000, None)
    names = set(explorer_output.files(out))
    assert f"data/wasabi2_opencoordinator/{MARCH}/coinjoin_tx_info.json.gz" in names
    assert "data/wasabi2_opencoordinator/false_cjtxs.json" in names
    assert "baseline/wasabi2/coinjoin_tx_info.json.gz" in names
    spend = json.loads(gzip.decompress((out / "curves/wasabi2_opencoordinator.spend.json.gz").read_bytes()))
    assert spend["schema_version"] == "fin-liquidity-1.0"
    assert spend["publicCurve"]["values"] == [0, 500, 500, 350]
    assert spend["publicCurve"]["txids"] == [None, "a", "b", "c"]
    assert spend["provenance"]["height"] == 886000


def test_output_holds_the_static_site_of_every_coordinator(scanner: Path, tmp_path: Path) -> None:
    html, html_fins = tmp_path / "explorer-html", tmp_path / "explorer-html-fins"
    for directory, selections in ((html, 0), (html_fins, 2)):
        (directory / "data/wasabi2_opencoordinator/images").mkdir(parents=True)
        (directory / "data/catalog.json").write_text(json.dumps({"selections": selections}))
        (directory / f"data/wasabi2_opencoordinator/{MARCH}.json").write_text('{"v": 1}')
        (directory / "data/wasabi2_opencoordinator/images/x.png").write_bytes(b"png")
    out = tmp_path / "out"
    explorer_output.build_output(scanner, out, 886000, html, html_fins)
    names = set(explorer_output.files(out))
    assert f"all/data/wasabi2_opencoordinator/{MARCH}.json.gz" in names
    assert {"all/catalog.json.gz", "all-fins/catalog.json.gz"} <= names
    # Each month once; the variants differ only in their catalog's saved selections.
    assert not any(name.startswith("all-fins/data/") or name.endswith(".png.gz") for name in names)
    fins = json.loads(gzip.decompress((out / "all-fins/catalog.json.gz").read_bytes()))
    assert fins == {"selections": 2}


def test_changes_upload_only_new_content() -> None:
    previous = {
        "schema_version": explorer_output.MANIFEST_SCHEMA,
        "files": {"same": {"size": 1, "sha256": "a"}, "changed": {"size": 1, "sha256": "b"}, "gone": {"size": 1, "sha256": "c"}},
    }
    current = {"same": {"size": 1, "sha256": "a"}, "changed": {"size": 1, "sha256": "x"}, "new": {"size": 1, "sha256": "n"}}
    assert explorer_output.changes(previous, current) == (["changed", "new"], ["gone"])
    # Without a manifest of this schema everything is uploaded again.
    assert explorer_output.changes({}, current)[0] == ["same", "changed", "new"]


def test_upload_commands_quote_paths_with_spaces(tmp_path: Path) -> None:
    name = f"data/pool/{MARCH}/coinjoin_tx_info.json.gz"
    commands = list(explorer_output.iter_commands([name], ["data/old.gz"], tmp_path, "s3://b/explorer"))
    assert commands == [f'cp "{tmp_path / name}" "s3://b/explorer/{name}"', 'rm "s3://b/explorer/data/old.gz"']


def test_prepare_scanner_puts_curated_inputs_where_coinjoin_analysis_reads_them(scanner: Path, tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    for protocol in ("wasabi1", "wasabi2", "whirlpool", "joinmarket"):
        (curated / protocol).mkdir(parents=True)
        (curated / protocol / "false_cjtxs.json").write_text("{}")
    (curated / "wasabi2" / "txid_coord.json").write_text("{}")
    (scanner / "Wasabi2CoinJoins.txt.json").write_text("stale")
    explorer_output.prepare_scanner(scanner, curated)
    assert (scanner / "joinmarket_all" / "false_cjtxs.json").is_file()
    assert (scanner / "wasabi2_others" / "txid_coord.json").is_file()
    assert (scanner / "wasabi2_others" / "false_cjtxs.json").is_file()
    assert not (scanner / "Wasabi2CoinJoins.txt.json").exists()


def test_detector_windows_become_explorer_selections_across_months(tmp_path: Path) -> None:
    from coinjoin_pipeline.execution.pbs.fin_windows import Window

    ordered = [(MARCH, ["a", "b"]), (APRIL, ["c", "d"])]
    places = explorer_output.month_places(ordered)
    setting = explorer_output.FIN_SETTINGS[0]
    # Samples 0..4 are [empty, a, b, c, d]; the window from sample 1 to 3 holds b and c.
    record = explorer_output.window_record("pool", setting, Window(1, 2, 3, 10**8, 10**8, True),
                                           [None, "a", "b", "c", "d"], ordered, places)
    assert record["month"] == MARCH and (record["first_txid"], record["last_txid"]) == ("b", "c")
    assert [(s["month"], s["start_index"], s["end_index"]) for s in record["month_segments"]] == [
        (MARCH, 1, 1), (APRIL, 0, 0)]
    assert record["num_coinjoins"] == 2 and record["annotation_type"] == "interval"
    again = explorer_output.window_record("pool", setting, Window(1, 2, 3, 10**8, 10**8, True),
                                          [None, "a", "b", "c", "d"], ordered, places)
    assert again["id"] == record["id"]
    assert "mix_enter_sats" not in record


def test_detector_windows_carry_the_fresh_and_leaving_bitcoin_of_their_coinjoins(scanner: Path) -> None:
    from coinjoin_pipeline.execution.pbs.fin_windows import Window

    flows: dict = {}
    ordered = explorer_output.explorer_months(scanner / "wasabi2_opencoordinator", flows)
    places = explorer_output.month_places(ordered)
    txids = [None] + [txid for _, month in ordered for txid in month]
    window = Window(0, 1, len(txids) - 1, 10**8, 10**8, True)
    record = explorer_output.window_record("pool", explorer_output.FIN_SETTINGS[0], window, txids, ordered, places, flows)
    selected = [flows[txid] for txid in txids[1:]]
    assert record["mix_enter_sats"] == sum(item[0] for item in selected) > 0
    assert record["mix_leave_sats"] == sum(item[1] for item in selected)
    assert record["liquidity_before_sats"] == 0
    assert record["liquidity_after_sats"] == selected[-1][3]
    # Each coinjoin continues from the liquidity the previous one left, across months.
    assert all(earlier[3] == later[2] for earlier, later in zip(selected, selected[1:]))


def test_intervals_end_at_the_newest_coinjoin_of_the_dumplings_files(tmp_path: Path) -> None:
    (tmp_path / "WasabiCoinJoins.txt").write_text("a:::x:::y:::1500000000:::z\nb:::x:::y:::1600000000:::z\n")
    (tmp_path / "Wasabi2CoinJoins.txt").write_text("c:::x:::y:::1660867286:::z\n")
    (tmp_path / "SamouraiCoinJoins.txt").write_text("")
    assert explorer_output.stop_date(tmp_path) == "2022-08-19 00:01:26.000"
    assert explorer_output.parse_options(["5M"], "2022-08-19 00:01:26.000") == (
        " --env_vars \"MIX_IDS=['5M'];interval_stop_date=2022-08-19 00:01:26.000\""
    )
    assert explorer_output.parse_options(None, None) == ""
    assert explorer_output.stop_date(tmp_path / "missing") is None


def test_explorer_order_skips_false_positives_and_sorts_by_relative_order(scanner: Path) -> None:
    ordered = explorer_output.explorer_months(scanner / "wasabi2_opencoordinator")
    assert ordered == [(MARCH, ["b", "a"]), (APRIL, ["c"])]

