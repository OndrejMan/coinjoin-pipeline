import shutil
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
    bootstrap = (EXECUTION_ROOT / "pbs/templates/stage_trap.sh").read_text()
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


def _run_stage(tmp_path, scratch_setup, preamble="", payload_status=0):
    bootstrap = (EXECUTION_ROOT / "pbs/templates/stage_trap.sh").read_text()
    script = f"""set -euo pipefail
cd "$1"
{scratch_setup}
DONE_MARKER=done
FAILED_MARKER=failed
{preamble}
stage_finalize() {{ echo outputs >> events; }}
publish_done() {{ test -f "$SCRATCHDIR/work/index" && echo done-with-scratch >> events; }}
publish_failed() {{ test -f "$SCRATCHDIR/work/index" && echo failed-with-scratch >> events; }}
{bootstrap}
exit {payload_status}
"""
    return subprocess.run(["bash", "-c", script, "test", str(tmp_path)], capture_output=True, text=True)


FILLED_SCRATCH = (
    'SCRATCHDIR="$1/scratch"; mkdir -p "$SCRATCHDIR/work"; touch "$SCRATCHDIR/work/index" "$SCRATCHDIR/.hidden"'
)


@pytest.mark.skipif(shutil.which("clean_scratch") is not None, reason="host provides clean_scratch")
@pytest.mark.parametrize("payload_status", [0, 7])
def test_scratch_contents_are_removed_after_markers_are_published(tmp_path, payload_status):
    result = _run_stage(tmp_path, FILLED_SCRATCH, payload_status=payload_status)

    assert result.returncode == payload_status, result.stderr
    marker = "done" if payload_status == 0 else "failed"
    assert (tmp_path / "events").read_text().splitlines() == ["outputs", f"{marker}-with-scratch"]
    assert (tmp_path / "scratch").is_dir()
    assert list((tmp_path / "scratch").iterdir()) == []


def test_scratch_cleanup_prefers_metacentrum_clean_scratch(tmp_path):
    preamble = "clean_scratch() { echo clean_scratch >> events; return 3; }"
    result = _run_stage(tmp_path, FILLED_SCRATCH, preamble=preamble)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "events").read_text().splitlines() == ["outputs", "done-with-scratch", "clean_scratch"]
    assert (tmp_path / "scratch" / "work" / "index").exists()


@pytest.mark.parametrize("scratch_setup", ["unset SCRATCHDIR", 'SCRATCHDIR="$1/missing"'])
def test_missing_scratch_does_not_change_the_exit_status(tmp_path, scratch_setup):
    (tmp_path / "keep").touch()
    bootstrap = (EXECUTION_ROOT / "pbs/templates/stage_trap.sh").read_text()
    script = f"""set -euo pipefail
cd "$1"
{scratch_setup}
DONE_MARKER=done
FAILED_MARKER=failed
stage_finalize() {{ :; }}
publish_done() {{ :; }}
publish_failed() {{ :; }}
{bootstrap}
exit 5
"""
    result = subprocess.run(["bash", "-c", script, "test", str(tmp_path)], capture_output=True, text=True)

    assert result.returncode == 5, result.stderr
    assert (tmp_path / "keep").exists()
