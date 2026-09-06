import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))

from client.pipeline_logging import StageLog  # noqa: E402


def read_only(directory: Path) -> None:
    """Make a directory that exists but cannot be written into."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o555)


class TestRelocate:
    def test_log_moves_into_the_destination(self, tmp_path: Path) -> None:
        stage_log = StageLog(tmp_path, "Docker emulation")
        stage_log.path.write_text("output", encoding="utf-8")

        destination = stage_log.relocate(tmp_path / "_failed")

        assert destination.parent.name == "_failed"
        assert destination.read_text(encoding="utf-8") == "output"
        assert stage_log.path == destination

    def test_log_stays_put_when_the_destination_is_not_writable(self, tmp_path: Path) -> None:
        stage_log = StageLog(tmp_path, "Docker emulation")
        stage_log.path.write_text("output", encoding="utf-8")
        original = stage_log.path
        read_only(tmp_path / "_failed")

        try:
            destination = stage_log.relocate(tmp_path / "_failed")
        finally:
            os.chmod(tmp_path / "_failed", 0o755)

        assert destination == original
        assert original.read_text(encoding="utf-8") == "output"

    def test_log_stays_put_when_the_destination_cannot_be_created(self, tmp_path: Path) -> None:
        stage_log = StageLog(tmp_path, "Docker emulation")
        stage_log.path.write_text("output", encoding="utf-8")
        original = stage_log.path
        read_only(tmp_path / "locked")

        try:
            destination = stage_log.relocate(tmp_path / "locked" / "_failed")
        finally:
            os.chmod(tmp_path / "locked", 0o755)

        assert destination == original
        assert original.exists()


class TestCapture:
    def test_failure_inside_the_stage_is_reported_unchanged(self, tmp_path: Path) -> None:
        stage_log = StageLog(tmp_path, "Docker emulation")
        read_only(tmp_path / "_failed")

        try:
            with pytest.raises(SystemExit) as failure:
                with stage_log.capture():
                    raise SystemExit(2)
        finally:
            os.chmod(tmp_path / "_failed", 0o755)

        assert failure.value.code == 2
        assert stage_log.path.parent.name == ".pending"

    def test_failure_is_filed_under_failed_when_the_move_works(self, tmp_path: Path) -> None:
        stage_log = StageLog(tmp_path, "Docker emulation")

        with pytest.raises(SystemExit):
            with stage_log.capture():
                raise SystemExit(2)

        assert stage_log.path.parent.name == "_failed"

    def test_captured_output_is_written_to_the_log(self, tmp_path: Path) -> None:
        stage_log = StageLog(tmp_path, "Docker emulation")

        with stage_log.capture():
            print("emulator output")

        assert "emulator output" in stage_log.path.read_text(encoding="utf-8")
