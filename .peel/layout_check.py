#!/usr/bin/env python3
"""Blank-line anomalies of Python files: 3+ blank lines, top-level defs with <2 blank lines
before them, a blank line right after a block opener. Prints one key per anomaly."""
import re
import subprocess
import sys

def anomalies(name, text):
    lines = text.split("\n")
    out = []
    run = 0
    for i, line in enumerate(lines):
        if line.strip() == "":
            run += 1
            continue
        if run >= 3:
            out.append(f"{name}: {run} blank lines before: {line.strip()[:60]}")
        if re.match(r"(def |class |async def |@)", line) and i > 0:
            j = i - 1
            while j >= 0 and lines[j].startswith("#"):
                j -= 1
            prev_blank = 0
            k = j
            while k >= 0 and lines[k].strip() == "":
                prev_blank += 1
                k -= 1
            if k >= 0 and not lines[k].startswith("@") and prev_blank < 2 and j == i - 1:
                out.append(f"{name}: {prev_blank} blank lines before top-level: {line.strip()[:60]}")
        if run >= 1 and i - run - 1 >= 0:
            opener = lines[i - run - 1]
            if opener.rstrip().endswith(":") and not opener.lstrip().startswith("#") and line.startswith(" "):
                out.append(f"{name}: blank line after opener: {opener.strip()[:60]}")
        run = 0
    return out

ref = sys.argv[1] if len(sys.argv) > 1 else None
files = subprocess.run(["git", "ls-files", "*.py"], capture_output=True, text=True).stdout.split()
found = set()
for name in files:
    found.update(anomalies(name, open(name).read()))
if ref:
    target = set()
    for name in files:
        shown = subprocess.run(["git", "show", f"{ref}:{name}"], capture_output=True, text=True)
        if shown.returncode == 0:
            target.update(anomalies(name, shown.stdout))
    found -= target
print("\n".join(sorted(found)) or "no new anomalies")
