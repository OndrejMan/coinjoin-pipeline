"""rebase --exec helper: the report mentions false-positive sidecars only while it applies them."""
import subprocess
from pathlib import Path

uses = subprocess.run(["git", "grep", "-l", "-i", "false_cjtx", "--", "pipeline/exporters", "src"],
                      capture_output=True, text=True).stdout.strip()
markdown = Path("pipeline/exporters/markdown_report.py")
old = (
    '    if "coinjoin_analysis" in detector_evaluations:\n'
    "        lines.extend(\n"
    "            [\n"
    '                "",\n'
    '                "> `coinjoin-analysis` metrics use the normalized baseline after applying the "\n'
    '                "false-positive sidecar files recorded in this report.",\n'
    "            ]\n"
    "        )\n"
    "\n"
)
if not uses and markdown.exists() and old in markdown.read_text():
    markdown.write_text(markdown.read_text().replace(old, ""))
test = Path("tests/pipeline/test_unified_report.py")
line = '        self.assertIn("coinjoin-analysis` metrics use the normalized baseline", markdown)\n'
if not uses and test.exists() and line in test.read_text():
    test.write_text(test.read_text().replace(line, ""))
