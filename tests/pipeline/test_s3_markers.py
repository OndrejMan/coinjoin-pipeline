import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from client.artifacts import S3Access
from client.s3_markers import cancel_dependent_pbs_job, wait_for_s3_pbs_marker


def test_wait_for_s3_pbs_marker_preserves_marker_and_probe_contract() -> None:
    calls: dict[str, object] = {}
    access = S3Access("https://s3.example", "/storage/user/credentials", "coinjoin")

    def wait_for_marker(*args: object, **kwargs: object) -> None:
        calls["args"] = args
        calls["kwargs"] = kwargs

    def pbs_probe(job_id: str):
        calls["job_id"] = job_id
        return lambda: "running"

    wait_for_s3_pbs_marker(
        stage="blocksci",
        job_id="123.server",
        run_prefix="s3://bucket/runs/run-1",
        access=access,
        walltime="01:00:00",
        wait_for_marker=wait_for_marker,
        pbs_probe=pbs_probe,
        wait_timeout=lambda _: 7200,
    )

    assert calls["args"] == (
        "blocksci",
        "s3://bucket/runs/run-1/.pbs/blocksci.done",
        "s3://bucket/runs/run-1/.pbs/blocksci.failed",
        access,
    )
    assert calls["kwargs"] == {"timeout_seconds": 7200, "probe": calls["kwargs"]["probe"]}
    assert calls["kwargs"]["probe"]() == "running"
    assert calls["job_id"] == "123.server"


def test_cancel_dependent_pbs_job_reports_manual_recovery(capsys) -> None:
    cancelled = cancel_dependent_pbs_job(
        "unified-report",
        "456.server",
        qdel_job=lambda _: False,
    )

    assert cancelled is False
    assert "cancel it with: qdel 456.server" in capsys.readouterr().err
