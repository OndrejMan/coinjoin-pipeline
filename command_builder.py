#!/usr/bin/env python3
"""Compatibility entrypoint for the packaged interactive command builder."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from coinjoin_pipeline.builder import *  # noqa: F401,F403,E402
from coinjoin_pipeline.builder import main  # noqa: E402


if __name__ == "__main__":
    main()
