"""rebase --exec helper: the six-condition pylint allowance exists only with the rule that needs it."""
from pathlib import Path

builder = Path("pipeline/exporters/report_builder.py")
pyproject = Path("pyproject.toml")
block = (
    "[tool.pylint.design]\n"
    "# The report builder's Wasabi threshold rule needs six conditions.\n"
    "max-bool-expr = 6\n"
    "\n"
)
if not (builder.exists() and "def build_warnings" in builder.read_text()) and block in pyproject.read_text():
    pyproject.write_text(pyproject.read_text().replace(block, ""))
