import subprocess

import pytest

from coinjoin_pipeline.paths import EXECUTION_ROOT


@pytest.mark.parametrize(
    "payload_status,transfer_status,marker_status,expected",
    [
        (0, 0, 0, 0),
        (7, 0, 0, 7),
        (0, 9, 0, 9),
        (7, 9, 0, 7),
        (0, 0, 8, 8),
    ],
)
def test_terminal_marker_follows_outputs_and_preserves_failure(
    tmp_path, payload_status, transfer_status, marker_status, expected
):
    bootstrap = (EXECUTION_ROOT / "pbs_stage.sh").read_text()
    script = f"""set -euo pipefail
cd "$1"
DONE_MARKER=done
FAILED_MARKER=failed
stage_finalize() {{ echo outputs >> events; upload_status={transfer_status}; }}
publish_done() {{ echo done >> events; return {marker_status}; }}
publish_failed() {{ echo failed >> events; }}
{bootstrap}
exit {payload_status}
"""
    result = subprocess.run(["bash", "-c", script, "test", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == expected
    events = (tmp_path / "events").read_text().splitlines()
    assert events[0] == "outputs"
    assert events[-1] == ("done" if expected == 0 else "failed")
