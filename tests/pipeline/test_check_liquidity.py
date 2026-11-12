"""The end-to-end liquidity checks accept a sound run and name what is wrong otherwise."""

import argparse
import csv
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "support" / "check_liquidity.py"
SPEC = importlib.util.spec_from_file_location("check_liquidity", SCRIPT)
check_liquidity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_liquidity)

AGREEING = {"inputs:MIX_ENTER->MIX_ENTER": 2, "outputs:MIX_LEAVE->MIX_LEAVE": 1, "outputs:MIX_STAY->MIX_STAY": 1}


def options(**flags):
    return argparse.Namespace(
        require_blocksci=flags.get("blocksci", False),
        require_postmix=flags.get("postmix", False),
        exact_baseline=flags.get("exact", False),
    )


def write_run(path, liquidity, events=4, coinjoins=1):
    path.mkdir(parents=True, exist_ok=True)
    (path / "unified_report.json").write_text(json.dumps({"liquidity_evaluation": liquidity}))
    (path / "liquidity_dataset.json").write_text(json.dumps({"coinjoins": [{}] * coinjoins}))
    with (path / "liquidity_events.csv").open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["coinjoin_index"])
        writer.writerows([[0]] * events)
    return path


def sound_liquidity():
    return {
        "available": True,
        "coinjoins": 1,
        "ground_truth": {"entered_sats": 1000, "left_sats": 400},
        "observed": {"entered_sats": 1000, "left_sats": 400},
        "interim_max_abs_difference_sats": 0,
        "classification_agreement": {"classified": 4, "agreement_rate": 1.0, "confusion": dict(AGREEING)},
        "postmix_spends": {"scheduled": 2, "by_status": {"sent": 2}, "confirmed_postmix": 2},
        "blocksci": {
            "available": True,
            "left_sats": 400,
            "classification_agreement": {"agreement_rate": 1.0},
        },
    }


def test_a_sound_postmix_run_passes_every_check(tmp_path):
    run = write_run(tmp_path, sound_liquidity())

    summary = check_liquidity.check(run, options(blocksci=True, postmix=True, exact=True))

    assert "1 coinjoins, 4 events" in summary
    assert "BlockSci agreement=1.0" in summary


@pytest.mark.parametrize(
    ("change", "flags", "message"),
    [
        (lambda liq: liq.update(available=False, reason="no labels"), {}, "evaluation unavailable: 'no labels'"),
        (lambda liq: liq["ground_truth"].update(entered_sats=0), {}, "no coinjoin inflow"),
        (lambda liq: liq["classification_agreement"].update(classified=5), {}, "4 rows, report classified 5"),
        (lambda liq: liq.pop("blocksci"), {"blocksci": True}, "exporter predates BlockSci liquidity"),
        (
            lambda liq: liq["blocksci"].update(available=False, reason="rebuild BlockSci"),
            {"blocksci": True},
            "BlockSci classification unavailable: 'rebuild BlockSci'",
        ),
        (
            lambda liq: liq["classification_agreement"].update(
                agreement_rate=0.75, confusion={**AGREEING, "outputs:MIX_REMIX->MIX_STAY": 1}
            ),
            {"exact": True},
            "{'outputs:MIX_REMIX->MIX_STAY': 1}",
        ),
        (lambda liq: liq.update(interim_max_abs_difference_sats=7), {"exact": True}, "differs by 7 sats"),
        (lambda liq: liq["postmix_spends"]["by_status"].update(sent=1, failed=1), {"postmix": True}, "was sent"),
        (lambda liq: liq["postmix_spends"].update(confirmed_postmix=1), {"postmix": True}, "only 1 of 2 spends"),
        (lambda liq: liq["observed"].update(left_sats=0), {"postmix": True}, "coinjoin-analysis classified no output"),
        (
            lambda liq: liq["blocksci"].update(left_sats=0),
            {"postmix": True, "blocksci": True},
            "BlockSci classified no output",
        ),
    ],
)
def test_each_broken_invariant_fails_with_its_reason(tmp_path, change, flags, message):
    liquidity = sound_liquidity()
    change(liquidity)
    run = write_run(tmp_path, liquidity)

    with pytest.raises(SystemExit, match="FAIL: liquidity") as failure:
        check_liquidity.check(run, options(**flags))

    assert message in str(failure.value)


def test_a_dataset_that_does_not_match_the_report_fails(tmp_path):
    run = write_run(tmp_path, sound_liquidity(), coinjoins=2)

    with pytest.raises(SystemExit, match="dataset has 2 coinjoins, report 1"):
        check_liquidity.check(run, options())


def test_a_missing_dataset_fails(tmp_path):
    run = write_run(tmp_path, sound_liquidity())
    (run / "liquidity_dataset.json").unlink()

    with pytest.raises(SystemExit, match="missing or empty"):
        check_liquidity.check(run, options())
