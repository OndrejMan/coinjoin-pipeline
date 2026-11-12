"""rebase --exec helper: once no analysis mode downloads an object, drop the object download path."""
import subprocess
from pathlib import Path

modes = Path("src/coinjoin_pipeline/execution/pbs/analysis_modes.py")
jobs = Path("src/coinjoin_pipeline/execution/pbs/blocksci_analysis_jobs.py")
settings = Path("src/coinjoin_pipeline/execution/pbs/stage_settings.py")
if modes.exists() and jobs.exists() and '"uri"' not in modes.read_text():
    changed = False
    old = '''    for download in settings["inputs"]:
        if "run" in download:
            stage.download(download["run"])
            continue
        target = stage.work / download["to"]
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            stage.storage.copy(download["uri"], str(target))
        except subprocess.CalledProcessError:
            if not download.get("optional"):
                raise
            stage.log(f"[{mode}] the baseline has no {target.name}")
'''
    new = '''    for download in settings["inputs"]:
        stage.download(download["run"])
'''
    text = jobs.read_text()
    if old in text:
        jobs.write_text(text.replace(old, new))
        changed = True
    old = '''class InputSettings(TypedDict, total=False):
    """One download before an analysis: a run directory, or an object placed at a run-relative path."""

    run: str
    uri: str
    to: str
    optional: bool
'''
    new = '''class InputSettings(TypedDict):
    """One download before an analysis: a run directory."""

    run: str
'''
    text = settings.read_text()
    if old in text:
        settings.write_text(text.replace(old, new))
        changed = True
    if changed:
        subprocess.run([".peel/fiximports.sh", str(jobs)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
        print("inputs:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
