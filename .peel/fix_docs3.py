"""rebase --exec helper: more docstrings and helpers that outlived the code they described."""
import subprocess
from pathlib import Path

changed = False


def text_of(path: str) -> str:
    p = Path(path)
    return p.read_text() if p.exists() else ""


def edit(path: str, old: str, new: str) -> None:
    global changed
    p = Path(path)
    if p.exists():
        text = p.read_text()
        if old in text:
            p.write_text(text.replace(old, new))
            changed = True


if "def ensure_no_active_s3_pbs_submission" not in text_of("src/coinjoin_pipeline/execution/locks.py"):
    edit(
        "src/coinjoin_pipeline/execution/locks.py",
        '"""Advisory command locks and S3 PBS overlap detection."""',
        '"""Advisory command locks."""',
    )
templates = text_of("src/coinjoin_pipeline/execution/pbs/templates_s3.py")
if templates and "def render_unified_report_s3_pbs" not in templates:
    edit(
        "pipeline/exporters/artifact_paths.py",
        "#: S3 runs only: the exporters staged into the run prefix for the BlockSci and report jobs.",
        "#: S3 runs only: the exporters staged into the run prefix for the BlockSci job.",
    )
    edit(
        "src/coinjoin_pipeline/execution/s3_staging.py",
        '    """The run\'s ``.pipeline/exporters/`` prefix that every BlockSci and report job downloads."""',
        '    """The run\'s ``.pipeline/exporters/`` prefix that every BlockSci job downloads."""',
    )
configuration = text_of("src/coinjoin_pipeline/configuration.py")
if "    def configured(self) -> bool:" in configuration and not any(
    ".configured" in text_of(str(path)) for path in Path("src").rglob("*.py")
):
    edit(
        "src/coinjoin_pipeline/configuration.py",
        "\n    @property\n    def configured(self) -> bool:\n        return any_option_set(self)\n",
        "",
    )
    if "any_option_set(" not in text_of("src/coinjoin_pipeline/configuration.py").replace("def any_option_set(", ""):
        subprocess.run(
            [".venv/bin/python", ".peel/peel.py", "def", "src/coinjoin_pipeline/configuration.py", "any_option_set"],
            check=True,
        )
if changed:
    subprocess.run([".peel/fiximports.sh", "src/coinjoin_pipeline/configuration.py"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
    print("docs3:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
