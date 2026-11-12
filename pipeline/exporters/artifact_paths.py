"""The run-directory layout: every relative path a run is made of, in one place.

The host package imports these names.
"""

from __future__ import annotations

EMULATOR_DIR = "coinjoin_emulator_data"
SCENARIO_FILE = EMULATOR_DIR + "/scenario.json"
# A run the emulator has finished.
EMULATION_MARKERS = (SCENARIO_FILE,)
