"""The fin-window detector of scenario-lab (``src/domain/fin-detector.ts``, trough-or-jump-rise-zone-v3)
for the mainnet Explorer data: windows of a coordinator's liquidity curve, which the baseline job
bakes into the CoinJoin Explorer page as saved selections.

A line-by-line port; tests/unit/test_fin_windows.py holds windows the TypeScript gives. Standard
library only and Python 3.8 syntax, like every module a stage job ships.
"""

from __future__ import annotations

import math
from typing import NamedTuple

ALGORITHM = "trough-or-jump-rise-zone-v3"
START_RULES = ("strongest-jump", "latest-sharp-entry")
# Share of the fall the first jump of a latest-sharp-entry start must reach.
ENTRY_SHARE = 0.5
# The Lab refuses longer curves; the detector is quadratic in the worst case.
MAX_SAMPLES = 100_000


class Window(NamedTuple):
    start: int
    peak: int
    end: int
    rise: int
    fall: int
    returned: bool


def required_height(baseline: int, height: int, percent: float) -> int:
    """The larger of both limits; a start at zero has no percentage, so the BTC limit decides."""
    return max(height, math.ceil(max(0, baseline) * percent / 100))


def detect(
    values: list[int],
    height: int = 50_000_000,
    duration: int = 2,
    percent: float = 0.0,
    start_rule: str = "strongest-jump",
) -> list[Window]:
    if len(values) > MAX_SAMPLES:
        raise ValueError(f"The fin detector supports at most {MAX_SAMPLES} samples")
    if height <= 0 or duration < 2 or percent < 0 or start_rule not in START_RULES:
        raise ValueError("Invalid fin detector settings")
    groups: dict[tuple[int, int], list[tuple[Window, int]]] = {}
    for start in range(len(values) - 2):
        baseline = values[start]
        need = required_height(baseline, height, percent)
        jump = values[start + 1] - baseline
        if jump <= 0 or (start > 0 and values[start - 1] < baseline and jump < need):
            continue
        peak = end = start + 1
        returned = False
        for index in range(start + 2, len(values)):
            if values[index] > values[peak]:
                if values[peak] - values[end] >= need:
                    break
                peak = end = index
            if values[index] < values[end]:
                end = index
            if values[index] <= baseline + need and values[peak] - values[index] >= need:
                while end + 1 < len(values) and values[end + 1] < values[end]:
                    end += 1
                returned = True
                break
        rise, fall = values[peak] - baseline, values[peak] - values[end]
        if rise < need or fall < need or end - start < duration:
            continue
        groups.setdefault((peak, end), []).append((Window(start, peak, end, rise, fall, returned), jump))
    found = []
    for starts in groups.values():
        strongest = starts[0]
        for candidate in starts[1:]:
            if candidate[1] > strongest[1]:
                strongest = candidate
        chosen = strongest
        if start_rule == "latest-sharp-entry":
            sharp = [item for item in starts if item[1] >= ENTRY_SHARE * item[0].fall and item[0].rise >= item[0].fall]
            chosen = sharp[-1] if sharp else strongest
        found.append(chosen[0])
    found.sort(key=lambda window: (window.start, -window.end))
    return found
