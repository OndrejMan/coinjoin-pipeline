#!/usr/bin/env python3
"""Remove whole top-level jobs (with the blank line before each) from a GitHub workflow."""
import re
import sys
from pathlib import Path

path, names = Path(sys.argv[1]), set(sys.argv[2:])
lines = path.read_text().splitlines(keepends=True)
starts = [i for i, line in enumerate(lines) if re.match(r"^  [a-z0-9-]+:\n$", line) and i > 0]
jobs_line = next(i for i, line in enumerate(lines) if line == "jobs:\n")
starts = [i for i in starts if i > jobs_line]
found = set()
keep = [True] * len(lines)
for index, start in enumerate(starts):
    name = lines[start].strip().rstrip(":")
    if name not in names:
        continue
    found.add(name)
    end = starts[index + 1] if index + 1 < len(starts) else len(lines)
    first = start
    # The comment lines directly above the job belong to it, then the blank separator.
    while first > 0 and lines[first - 1].lstrip().startswith("#"):
        first -= 1
    if first > 0 and lines[first - 1].strip() == "":
        first -= 1
    for i in range(first, end):
        keep[i] = False
missing = names - found
if missing:
    sys.exit(f"jobs not found: {sorted(missing)}")
text = "".join(line for line, kept in zip(lines, keep) if kept)
path.write_text(text.rstrip("\n") + "\n")
