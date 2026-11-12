"""rebase --exec helper: the s3 module docstring names only modules that still hold what it says."""
import subprocess
from pathlib import Path

s3 = Path("src/coinjoin_pipeline/storage/s3.py")
if s3.exists():
    text = s3.read_text()
    markers = Path("src/coinjoin_pipeline/execution/s3_markers.py")
    staging = Path("src/coinjoin_pipeline/execution/s3_staging.py")
    waits = markers.exists() and "def wait_for_s3_marker" in markers.read_text()
    stages_exporters = staging.exists() and "def upload_exporters" in staging.read_text()
    full = """

Marker waiting lives in ``execution/s3_markers.py`` and exporter staging in
``execution/s3_staging.py``.
"""
    staging_only = """

Exporter staging lives in ``execution/s3_staging.py``.
"""
    wanted = full if waits else staging_only if stages_exporters else ""
    new = text
    for variant in (full, staging_only):
        if variant in new and variant != wanted:
            new = new.replace(variant, wanted)
    # A one-line docstring the previous run left open.
    new = new.replace('access to them.\n"""', 'access to them."""')
    if new != text:
        s3.write_text(new)
        subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
        print("s3doc:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
