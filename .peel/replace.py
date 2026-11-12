#!/usr/bin/env python3
"""Exact, unique text replacements read from stdin.

Format (repeatable):
@@@ path/to/file
<old text>
===
<new text>
@@@ end
Each old text must occur exactly once in its file. New text may be empty.
"""
import re
import sys
from pathlib import Path

data = sys.stdin.read()
blocks = re.findall(r"^@@@ (.+?)\n(.*?)^@@@ end\n", data, flags=re.S | re.M)
if not blocks:
    sys.exit("no blocks")
texts = {}
for name, body in blocks:
    if "\n===\n" in body:
        old, new = body.split("\n===\n", 1)
        # The last line of either side may be partial; an empty side deletes whole lines.
        new = new[:-1] if new.endswith("\n") else new
        if new == "":
            old += "\n"
    elif body.startswith("===\n"):
        sys.exit(f"{name}: empty old text")
    else:
        sys.exit(f"{name}: missing ===")
    path = Path(name.strip())
    text = texts.get(path) if path in texts else path.read_text()
    count = text.count(old)
    if count != 1:
        sys.exit(f"{path}: old text occurs {count} times (nothing written):\n{old}")
    texts[path] = text.replace(old, new)
for path, text in texts.items():
    path.write_text(text)
print(f"{len(blocks)} replacement(s)")
