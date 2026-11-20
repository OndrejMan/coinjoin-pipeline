"""The Python port of scenario-lab's fin-window detector gives the TypeScript's windows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coinjoin_pipeline.execution.pbs import fin_windows

# Windows of src/domain/fin-detector.ts on two recorded emulation curves, in the settings order below.
FIXTURE = json.loads((Path(__file__).parent / "fixtures/fin_windows_typescript.json").read_text())
SETTINGS = (
    {},
    {"percent": 15},
    {"start_rule": "latest-sharp-entry"},
    {"percent": 15, "start_rule": "latest-sharp-entry"},
)


@pytest.mark.parametrize("run", sorted(FIXTURE))
@pytest.mark.parametrize("setting", range(len(SETTINGS)))
def test_windows_match_the_typescript_detector(run: str, setting: int) -> None:
    found = fin_windows.detect(FIXTURE[run]["values"], **SETTINGS[setting])
    assert [[w.start, w.peak, w.end] for w in found] == FIXTURE[run]["windows"][setting]


def test_percentage_raises_the_threshold_only_above_the_btc_limit() -> None:
    assert fin_windows.required_height(0, 50_000_000, 15) == 50_000_000
    assert fin_windows.required_height(1_000_000_000, 50_000_000, 15) == 150_000_000


def test_refuses_curves_beyond_the_labs_limit_and_unknown_settings() -> None:
    with pytest.raises(ValueError, match="at most"):
        fin_windows.detect([0] * (fin_windows.MAX_SAMPLES + 1))
    with pytest.raises(ValueError, match="settings"):
        fin_windows.detect([0, 1, 0], start_rule="other")
