"""Reject the runner configuration that exhausted inotify during Podman emulation."""

from pathlib import Path
import runpy

import pytest

check = runpy.run_path(str(Path(__file__).resolve().parents[1] / "support/check_inotify.py"))["main"]


@pytest.mark.parametrize("instances,watches", [(128, 65536), (128, 524288), (1024, 65536)])
def test_insufficient_host_limits_fail_with_the_correct_remedy(tmp_path, capsys, instances, watches):
    (tmp_path / "max_user_instances").write_text(str(instances))
    (tmp_path / "max_user_watches").write_text(str(watches))

    assert check(tmp_path) == 1
    output = capsys.readouterr().out
    assert "too many open files" in output
    assert "sudo sysctl -w fs.inotify.max_user_instances=1024 fs.inotify.max_user_watches=524288" in output


def test_sufficient_host_limits_pass_without_changing_them(tmp_path):
    (tmp_path / "max_user_instances").write_text("2048")
    (tmp_path / "max_user_watches").write_text("1048576")

    assert check(tmp_path) == 0
    assert (tmp_path / "max_user_instances").read_text() == "2048"
    assert (tmp_path / "max_user_watches").read_text() == "1048576"


def test_unreadable_or_invalid_limits_fail(tmp_path, capsys):
    (tmp_path / "max_user_instances").write_text("invalid")

    assert check(tmp_path) == 1
    assert "cannot read fs.inotify.max_user_instances" in capsys.readouterr().out


def test_non_linux_host_has_no_local_inotify_check(tmp_path):
    assert check(tmp_path / "missing") == 0
