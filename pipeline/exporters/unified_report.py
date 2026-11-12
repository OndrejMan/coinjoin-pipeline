#!/usr/bin/env python3
"""Executable entrypoint for report assembly from persisted analyzer results."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporters.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
