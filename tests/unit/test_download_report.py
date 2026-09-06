from __future__ import annotations

from pathlib import Path
import subprocess
from unittest import mock

import pytest

from coinjoin_pipeline.download_report import (
    DownloadError,
    S3Access,
    download_report,
    validate_output_directory,
)


ACCESS = S3Access(
    endpoint_url="https://s3.example.test",
    credentials_file=Path("/storage/user/.aws/credentials"),
    profile="coinjoin",
)


def test_output_directory_rejects_protected_and_unrecognized_paths(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    unrecognized = tmp_path / "existing"
    unrecognized.mkdir()
    report = tmp_path / "report"
    report.mkdir()
    (report / "unified_report.json").write_text("{}", encoding="utf-8")
    symlink = tmp_path / "report-link"
    symlink.symlink_to(report, target_is_directory=True)

    for path in (
        Path("/"),
        Path.home(),
        Path.cwd(),
        runs_root,
        unrecognized,
        symlink,
    ):
        with pytest.raises(ValueError, match="refusing|recognized|symbolic"):
            validate_output_directory(path, runs_root)


def test_output_directory_allows_new_or_recognized_report_paths(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    existing = tmp_path / "report"
    existing.mkdir()
    (existing / "unified_report.json").write_text("{}", encoding="utf-8")

    assert validate_output_directory(tmp_path / "new-report", runs_root) == (
        tmp_path / "new-report"
    )
    assert validate_output_directory(existing, runs_root) == existing


def test_download_requires_completed_report_marker(tmp_path: Path) -> None:
    with mock.patch(
        "coinjoin_pipeline.download_report._object_exists",
        side_effect=[False, False],
    ):
        with pytest.raises(DownloadError, match="not complete"):
            download_report(ACCESS, "s3://bucket/runs", "run-1", tmp_path)


def test_download_reports_remote_failure_marker(tmp_path: Path) -> None:
    with mock.patch(
        "coinjoin_pipeline.download_report._object_exists", return_value=True
    ):
        with pytest.raises(DownloadError, match="recorded failure"):
            download_report(ACCESS, "s3://bucket/runs", "run-1", tmp_path)


def test_download_syncs_canonical_report_directory(tmp_path: Path) -> None:
    def fake_s5cmd(
        _access: S3Access, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        assert arguments[:2] == (
            "sync",
            "s3://bucket/runs/run-1/coinjoinPipeline_data/",
        )
        staging_dir = Path(arguments[2])
        assert staging_dir != tmp_path
        (staging_dir / "unified_report.json").write_text("{}", encoding="utf-8")
        (staging_dir / "unified_report.md").write_text("# Report", encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    with (
        mock.patch(
            "coinjoin_pipeline.download_report._object_exists",
            side_effect=[False, True],
        ),
        mock.patch(
            "coinjoin_pipeline.download_report._run_s5cmd",
            side_effect=fake_s5cmd,
        ),
    ):
        json_report, markdown_report = download_report(
            ACCESS, "s3://bucket/runs", "run-1", tmp_path
        )

    assert json_report == tmp_path / "unified_report.json"
    assert markdown_report == tmp_path / "unified_report.md"


def test_download_does_not_accept_stale_destination_json(tmp_path: Path) -> None:
    output_dir = tmp_path / "report"
    output_dir.mkdir()
    stale_json = output_dir / "unified_report.json"
    stale_json.write_text('{"run": "old"}', encoding="utf-8")

    with (
        mock.patch(
            "coinjoin_pipeline.download_report._object_exists",
            side_effect=[False, True],
        ),
        mock.patch(
            "coinjoin_pipeline.download_report._run_s5cmd",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
        pytest.raises(DownloadError, match="unified_report.json is missing"),
    ):
        download_report(
            ACCESS,
            "s3://bucket/runs",
            "run-1",
            output_dir,
        )

    assert stale_json.read_text(encoding="utf-8") == '{"run": "old"}'


def test_download_removes_stale_markdown_when_remote_has_none(tmp_path: Path) -> None:
    output_dir = tmp_path / "report"
    output_dir.mkdir()
    stale_markdown = output_dir / "unified_report.md"
    stale_markdown.write_text("# Old report", encoding="utf-8")

    def fake_s5cmd(
        _access: S3Access, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        staging_dir = Path(arguments[2])
        (staging_dir / "unified_report.json").write_text(
            '{"run": "new"}', encoding="utf-8"
        )
        return subprocess.CompletedProcess(arguments, 0, "", "")

    with (
        mock.patch(
            "coinjoin_pipeline.download_report._object_exists",
            side_effect=[False, True],
        ),
        mock.patch(
            "coinjoin_pipeline.download_report._run_s5cmd",
            side_effect=fake_s5cmd,
        ),
    ):
        json_report, markdown_report = download_report(
            ACCESS,
            "s3://bucket/runs",
            "run-1",
            output_dir,
        )

    assert json_report.read_text(encoding="utf-8") == '{"run": "new"}'
    assert markdown_report is None
    assert not stale_markdown.exists()


def test_download_replaces_destination_instead_of_merging(tmp_path: Path) -> None:
    output_dir = tmp_path / "report"
    output_dir.mkdir()
    stale_file = output_dir / "old-supplement.json"
    stale_file.write_text("old", encoding="utf-8")

    def fake_s5cmd(
        _access: S3Access, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        staging_dir = Path(arguments[2])
        (staging_dir / "unified_report.json").write_text(
            '{"run": "new"}', encoding="utf-8"
        )
        return subprocess.CompletedProcess(arguments, 0, "", "")

    with (
        mock.patch(
            "coinjoin_pipeline.download_report._object_exists",
            side_effect=[False, True],
        ),
        mock.patch(
            "coinjoin_pipeline.download_report._run_s5cmd",
            side_effect=fake_s5cmd,
        ),
    ):
        download_report(ACCESS, "s3://bucket/runs", "run-1", output_dir)

    assert not stale_file.exists()
