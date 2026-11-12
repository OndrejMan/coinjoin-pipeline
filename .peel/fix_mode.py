"""rebase --exec helper: artifact fixtures keep a "mode" key only while the artifact records one."""
from pathlib import Path

if 'get("mode")' not in Path("pipeline/exporters/analysis_artifact.py").read_text():
    for name in ("tests/pipeline/test_blocksci_analysis.py", "tests/pipeline/test_refactor_regressions.py"):
        p = Path(name)
        if p.exists():
            p.write_text(p.read_text().replace('                "mode": "emulator",\n', ""))
