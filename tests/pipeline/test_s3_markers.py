import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from coinjoin_pipeline.execution import s3_markers
from coinjoin_pipeline.execution.s3_markers import (
    cancel_dependent_pbs_job,
    wait_for_s3_pbs_stage,
)
from coinjoin_pipeline.storage.s3 import S3Access


def test_wait_for_s3_pbs_stage_preserves_marker_and_probe_contract(monkeypatch) -> None:
    calls: dict[str, object] = {}
    access = S3Access("https://s3.example", "/storage/user/credentials", "coinjoin")

    def wait_for_marker(*args: object, **kwargs: object) -> None:
        calls["args"] = args
        calls["kwargs"] = kwargs

    def pbs_probe(job_id: str):
        calls["job_id"] = job_id
        return lambda: "running"

    monkeypatch.setattr(s3_markers, "wait_for_s3_marker", wait_for_marker)
    monkeypatch.setattr(s3_markers, "pbs_job_probe", pbs_probe)
    monkeypatch.setattr(s3_markers, "pbs_wait_timeout", lambda _: 7200)

    wait_for_s3_pbs_stage(
        stage="blocksci",
        job_id="123.server",
        run_prefix="s3://bucket/runs/run-1",
        access=access,
        walltime="01:00:00",
    )

    assert calls["args"] == (
        "blocksci",
        "s3://bucket/runs/run-1/.pbs/blocksci.done",
        "s3://bucket/runs/run-1/.pbs/blocksci.failed",
        access,
    )
    assert calls["kwargs"] == {
        "timeout_seconds": 7200,
        "probe": calls["kwargs"]["probe"],
    }
    assert calls["kwargs"]["probe"]() == "running"
    assert calls["job_id"] == "123.server"


def test_cancel_dependent_pbs_job_reports_manual_recovery(monkeypatch, capsys) -> None:
    monkeypatch.setattr(s3_markers, "qdel_pbs_job", lambda _: False)

    cancelled = cancel_dependent_pbs_job("unified-report", "456.server")

    assert cancelled is False
    assert "cancel it with: qdel 456.server" in capsys.readouterr().err
