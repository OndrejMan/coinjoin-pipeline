"""Read back what a rendered PBS job embeds; tests alone need the inverse of write_files."""

from coinjoin_pipeline.execution.pbs.scripts import heredoc_delimiter


def embedded_file(script: str, name: str) -> str:
    """The content write_files embedded for name, read back from a rendered script."""
    delimiter = heredoc_delimiter(name)
    return script.split(f"<<'{delimiter}'\n", 1)[1].split(f"\n{delimiter}\n", 1)[0]
