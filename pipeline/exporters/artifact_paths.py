"""The run-directory layout: every relative path a run is made of, in one place.

The host package imports these names.
"""

from __future__ import annotations

COINJOIN_ANALYSIS_DIR = "coinjoin-analysis_data"
EMULATOR_DIR = "coinjoin_emulator_data"
SCENARIO_FILE = EMULATOR_DIR + "/scenario.json"
# A run the emulator has finished.
EMULATION_MARKERS = (SCENARIO_FILE,)
