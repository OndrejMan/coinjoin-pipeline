"""rebase --exec helper: report input hashes exist only while `runs list` marks stale reports with them."""
import re
import subprocess
import sys
from pathlib import Path

if Path("src/coinjoin_pipeline/execution/run_catalog.py").exists():
    raise SystemExit(0)

PEEL = [sys.executable, ".peel/peel.py"]


def peel(*args: str) -> None:
    subprocess.run([*PEEL, *args], check=True)


paths = Path("pipeline/exporters/artifact_paths.py")
if "REPORT_INPUT_FILES = (" in paths.read_text():
    peel("def", str(paths), "REPORT_INPUT_FILES", "report_input_files")

manifest = Path("pipeline/exporters/manifest.py")
text = manifest.read_text()
if '        "inputs": report_input_hashes(run_dir),\n' in text:
    manifest.write_text(text.replace('        "inputs": report_input_hashes(run_dir),\n', ""))
if "def report_input_hashes" in manifest.read_text():
    peel("def", str(manifest), "report_input_hashes")
    peel("import", str(manifest), "report_input_files")
    if "file_sha256(" not in manifest.read_text():
        peel("import", str(manifest), "file_sha256")

types = Path("pipeline/exporters/report_types.py")
text = types.read_text()
text = re.sub(
    r"    # SHA-256 of each report input by run-relative path; `runs list` compares it to mark\n"
    r"    # a report stale\.\n    inputs: dict\[str, str\]\n",
    "",
    text,
)
lone = re.search(
    r'\n\n\nclass RunManifest\(_RunManifestFields, total=False\):\n    ("""[^\n]*""")\n\n(?=\n\nclass )', text
)
if lone:
    text = text.replace(lone.group(0), "\n")
    text = text.replace("class _RunManifestFields(TypedDict):\n", f"class RunManifest(TypedDict):\n    {lone.group(1)}\n\n")
types.write_text(text)

test = Path("tests/pipeline/test_report_assembly.py")
if test.exists():
    text = test.read_text()
    old = (
        "    # The snapshot shapes the coverage, so changing it must mark the report stale.\n"
        '    assert DUMPLINGS_SNAPSHOT_FILE in report["run_manifest"]["inputs"]\n'
    )
    if old in text:
        text = text.replace(old, "").replace(
            "def test_external_report_records_window_snapshot_and_its_inputs(",
            "def test_external_report_records_the_window_snapshot(",
        )
        test.write_text(text)
        subprocess.run([".venv/bin/ruff", "check", "-q", "--select", "F401", "--fix", str(test)], check=False)

for name in ("ANALYSIS_ARTIFACT_FILE", "LABEL_MANIFEST_FILE", "ROUND_EVENTS_FILE", "DUMPLINGS_SNAPSHOT_FILE", "MAPPINGS_FILE"):
    if not re.search(rf"^{name} = ", paths.read_text(), flags=re.M):
        continue
    uses = subprocess.run(["git", "grep", "-c", "-w", name, "--", "src", "pipeline", "tests", "scripts"],
                          capture_output=True, text=True).stdout
    if sum(int(line.rsplit(":", 1)[1]) for line in uses.splitlines()) == 1:
        peel("def", str(paths), name)
