"""rebase --exec helper: _print_dry_run names only PBS actions that still exist."""
import re
import subprocess
from pathlib import Path

orchestrator = Path("src/coinjoin_pipeline/execution/orchestrator.py")
configuration = Path("src/coinjoin_pipeline/configuration.py")
if orchestrator.exists() and configuration.exists():
    text = orchestrator.read_text()
    actions = re.search(r"PipelineAction = Literal\[(.*?)\]", configuration.read_text(), re.S).group(1)
    shared = Path("src/coinjoin_pipeline/execution/shared_storage_pbs.py").exists()
    start = text.find("    use_pbs_dry_run = (\n")
    end = text.find("\n    )\n", start)
    if start >= 0 and end >= 0:
        lines = text[start:end].split("\n")[1:]
        terms, current = [], []
        for line in lines:
            body = line.strip()
            if body.startswith("or ") and current:
                terms.append(" ".join(current))
                current = [body[3:]]
            else:
                current.append(body)
        terms.append(" ".join(current))
        keep = []
        for term in terms:
            if not shared and ('args.action == "analyze"' in term or 'args.action == "coinjoin-analysis"' in term):
                continue
            if '"mappings"' in term and '"mappings"' not in actions:
                continue
            keep.append(term)
        if keep != terms:
            rendered = []
            for index, term in enumerate(keep):
                if term.startswith("(") and term.endswith(")") and len(keep) == 1:
                    term = term[1:-1]
                if index == 0 and term.startswith("(") and term.endswith(")") and term.count("(") == 1:
                    term = term[1:-1]
                rendered.append(("        " if index == 0 else "        or ") + term)
            new = "    use_pbs_dry_run = (\n" + "\n".join(rendered)
            orchestrator.write_text(text[:start] + new + text[end:])
            subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
            print("dry-run:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
