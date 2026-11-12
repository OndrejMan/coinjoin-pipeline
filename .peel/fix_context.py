"""rebase --exec helper: an S3 run resolves the report image only while a report job exists."""
import subprocess
from pathlib import Path

context = Path("src/coinjoin_pipeline/context.py")
templates = Path("src/coinjoin_pipeline/execution/pbs/templates_s3.py")
has_report_job = templates.exists() and "def render_unified_report_s3_pbs" in templates.read_text()
if context.exists() and not has_report_job:
    text = context.read_text()
    old = """        from .execution.pbs_settings import resolve_unified_report_image, resolve_uploader_image

        config = replace(
            config,
            images=replace(
                config.images,
                uploader=resolve_uploader_image(config),
                unified_report=resolve_unified_report_image(config),
            ),
        )
"""
    new = """        from .execution.pbs_settings import resolve_uploader_image

        config = replace(config, images=replace(config.images, uploader=resolve_uploader_image(config)))
"""
    if old in text:
        context.write_text(text.replace(old, new))
        subprocess.run([".venv/bin/ruff", "format", "-q", str(context)], check=True)
        subprocess.run(["git", "commit", "-qa", "--amend", "--no-edit"], check=True)
        print("context:", subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip())
