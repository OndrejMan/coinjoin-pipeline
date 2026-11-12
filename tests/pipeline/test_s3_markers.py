import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from coinjoin_pipeline.execution import s3_markers


def test_rollback_continues_past_a_failing_qdel_and_returns_the_survivors(monkeypatch, capsys) -> None:
    attempted = []

    def qdel(job_id: str) -> bool:
        attempted.append(job_id)
        if job_id == "2.server":
            raise OSError("qdel could not be spawned")
        return job_id != "3.server"

    monkeypatch.setattr(s3_markers, "qdel_pbs_job", qdel)

    failed = s3_markers.rollback_s3_pbs_submissions([("a", "1.server"), ("b", "2.server"), ("c", "3.server")])

    assert attempted == ["3.server", "2.server", "1.server"]
    assert failed == [("c", "3.server"), ("b", "2.server")]
    assert "ROLLBACK INCOMPLETE" in capsys.readouterr().err
