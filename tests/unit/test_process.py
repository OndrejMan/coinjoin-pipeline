from pathlib import Path

from coinjoin_pipeline.process import run


def test_a_command_that_cannot_start_is_reported_with_its_reason(tmp_path: Path, capsys) -> None:
    script = tmp_path / "not-executable"
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    assert run([str(tmp_path / "missing")]) == 127
    assert "missing: No such file or directory" in capsys.readouterr().err
    assert run([str(script)]) == 127
    assert "not-executable: Permission denied" in capsys.readouterr().err
