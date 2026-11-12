"""rebase --exec helper: drop the stored-run fixture once no test in its module uses it."""
import subprocess
import sys
from pathlib import Path

test = Path("tests/pipeline/test_report_assembly.py")


def peel(name: str) -> None:
    subprocess.run([sys.executable, ".peel/peel.py", "def", str(test), name], check=True)


if test.exists() and "def stored_run(" in test.read_text() and test.read_text().count("stored_run(") == 1:
    peel("stored_run")
    if test.read_text().count("write(") == 1:
        peel("write")
    if test.read_text().count("TXID") == 1:
        peel("TXID")
    subprocess.run([".venv/bin/ruff", "check", "-q", "--select", "F401", "--fix", str(test)], check=False)
    text = test.read_text()
    if "report_cli" not in text:
        text = text.replace(
            '"""Report assembly through the exporter CLI and the helpers its sections share."""',
            '"""The record helpers the report sections share."""',
        )
        test.write_text(text)
    subprocess.run([".venv/bin/ruff", "format", "-q", str(test)], check=False)
