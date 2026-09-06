import json
from pathlib import Path

from coinjoin_pipeline.commands import metadata

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = REPOSITORY_ROOT / "command_metadata.json"


def test_packaged_command_metadata_matches_the_generated_snapshot() -> None:
    """The packaged copy is what validates argv; a stale one rejects live flags.

    ``scripts/generate-command-metadata.py`` writes the repository snapshot by
    default, but ``commands.metadata()`` reads the packaged one, so regenerating
    only the former silently makes new options unusable.
    """
    assert metadata() == json.loads(CANONICAL.read_text(encoding="utf-8"))


def test_packaged_command_metadata_covers_the_image_override_flags() -> None:
    commands = metadata()["commands"]
    for action in ("emulate", "full-run", "pbs-from-s3"):
        options = commands[action]["options"]
        assert "--uploader-image" in options
        assert "--unified-report-image" in options
