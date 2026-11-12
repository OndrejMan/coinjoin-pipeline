"""rebase --exec helper: the manifest is "diffed" only while reports compare manifests."""
from pathlib import Path

manifest = Path("pipeline/exporters/manifest.py")
types = Path("pipeline/exporters/report_types.py")
if manifest.exists() and "def compare_run_manifests" not in manifest.read_text():
    old = "is the provenance two runs\nare diffed by.  Declaring"
    new = "is the provenance a report\nkeeps.  Declaring"
    text = types.read_text()
    if old in text:
        types.write_text(text.replace(old, new))
