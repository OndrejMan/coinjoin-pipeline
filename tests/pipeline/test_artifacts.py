import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))

from client.artifacts import (  # noqa: E402
    render_s5cmd_sync,
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
)


def test_validates_s3_compatible_parameters() -> None:
    assert validate_artifact_uri("s3://bucket/runs/") == "s3://bucket/runs"
    assert validate_run_id("wasabi-test_001.2") == "wasabi-test_001.2"
    assert (
        validate_s3_endpoint_url("https://s3.cl4.du.cesnet.cz/")
        == "https://s3.cl4.du.cesnet.cz"
    )
    assert validate_credentials_file("/storage/user/.aws/credentials").startswith(
        "/storage/"
    )


@pytest.mark.parametrize("run_id", ["", "../run", "run/id", "run id", "run;id", "a..b"])
def test_rejects_unsafe_run_ids(run_id: str) -> None:
    with pytest.raises(ValueError):
        validate_run_id(run_id)


def test_render_uses_only_s5cmd_and_disables_environment_fallback() -> None:
    command = render_s5cmd_sync('"$SRC"', '"$DST"')
    assert "s5cmd --credentials-file" in command
    assert "--profile" in command and "--endpoint-url" in command
    assert "env -u AWS_ACCESS_KEY_ID" in command
    assert "aws s3" not in command
    assert "s3cmd" not in command
