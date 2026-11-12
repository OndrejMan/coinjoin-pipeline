"""cjp status and cjp stop read and act on the records the pipeline itself keeps."""

import json
import subprocess
from pathlib import Path

import pytest

from coinjoin_pipeline import status, stop

QSTAT = """Job Id: 11.pbs-m1.metacentrum.cz
    Job_Name = blocksci
    job_state = R
Job Id: 12.pbs-m1.metacentrum.cz
    Job_Name = report
    job_state = H
"""
JOBS = {
    "items": [
        {"metadata": {"labels": {"coinjoin.run-id": "run-1"}}, "status": {"active": 1}},
        {
            "metadata": {"labels": {"coinjoin.run-id": "run-2"}},
            "status": {"conditions": [{"type": "Failed", "status": "True"}]},
        },
    ]
}


def _record(root: Path, run_id: str, stage: str, job_id: str) -> None:
    path = root / run_id / ".pbs" / f"{stage}.jobid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(job_id + "\n")


@pytest.fixture
def commands(monkeypatch):
    """Answer qstat, kubectl and qdel like a frontend with two runs."""
    seen: list[list[str]] = []

    def run(command, **kwargs):
        seen.append(command)
        stdout = ""
        if command[0] == "qstat":
            stdout = QSTAT
        elif "get" in command:
            stdout = json.dumps(JOBS)
        elif "logs" in command:
            kwargs["stdout"].write("pod log\n")
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    return seen


def test_status_joins_recorded_jobs_with_one_scheduler_and_one_kubernetes_query(tmp_path, commands, capsys) -> None:
    _record(tmp_path, "run-1", "blocksci", "11.pbs-m1.metacentrum.cz")
    _record(tmp_path, "run-1", "unified-report", "12.pbs-m1.metacentrum.cz")
    _record(tmp_path, "run-2", "coinjoin-analysis", "9.pbs-m1.metacentrum.cz")

    code = status.main(["--run-id", "run-1", "--run-id", "run-2", "--json"], runs_root=tmp_path)

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["warnings"] == []
    assert report["runs"]["run-1"] == {
        "jobIds": {"blocksci": "11.pbs-m1.metacentrum.cz", "unified-report": "12.pbs-m1.metacentrum.cz"},
        "pbs": {"blocksci": "running", "unified-report": "queued"},
        "emulation": "running",
    }
    # A job missing from the listing has left the queue.
    assert report["runs"]["run-2"]["pbs"] == {"coinjoin-analysis": "finished"}
    assert report["runs"]["run-2"]["emulation"] == "failed"
    assert [command[0] for command in commands] == ["qstat", "kubectl"]


def test_status_searches_roots_in_order_and_skips_invalid_records(tmp_path, commands, capsys) -> None:
    first, second = tmp_path / "lab", tmp_path / "checkout"
    _record(first, "run-1", "blocksci", "11.pbs-m1.metacentrum.cz")
    _record(second, "run-1", "blocksci", "99.pbs-m1.metacentrum.cz")
    _record(second, "run-1", "coinjoin-analysis", "bad id;")

    status.main(
        ["--run-id", "run-1", "--search-root", str(first), "--search-root", str(second), "--json", "--no-kubernetes"]
    )

    report = json.loads(capsys.readouterr().out)
    assert report["runs"]["run-1"]["jobIds"] == {"blocksci": "11.pbs-m1.metacentrum.cz"}
    assert "Invalid PBS job ID" in report["warnings"][0]


def test_status_reports_an_unavailable_scheduler_instead_of_guessing(tmp_path, monkeypatch, capsys) -> None:
    _record(tmp_path, "run-1", "blocksci", "11.pbs-m1.metacentrum.cz")
    monkeypatch.setattr(status, "scheduler_states", lambda: None)

    status.main(["--run-id", "run-1", "--json", "--no-kubernetes"], runs_root=tmp_path)

    report = json.loads(capsys.readouterr().out)
    assert "pbs" not in report["runs"]["run-1"]
    assert report["warnings"] == ["PBS status is unavailable."]


def test_status_rejects_an_invalid_run_id(tmp_path, capsys) -> None:
    assert status.main(["--run-id", "../escape"], runs_root=tmp_path) != 0
    assert "run ID" in capsys.readouterr().err


def test_stop_saves_pod_logs_deletes_labelled_resources_and_cancels_every_job(tmp_path, commands, capsys) -> None:
    _record(tmp_path, "run-1", "blocksci", "11.pbs-m1.metacentrum.cz")
    _record(tmp_path, "run-1", "unified-report", "12.pbs-m1.metacentrum.cz")
    log = tmp_path / "stop-pods.log"

    code = stop.main(
        ["--run-id", "run-1", "--namespace", "ns", "--save-pod-logs", str(log), "--json"], runs_root=tmp_path
    )

    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "runId": "run-1",
        "complete": True,
        "cancelled": {"blocksci": "11.pbs-m1.metacentrum.cz", "unified-report": "12.pbs-m1.metacentrum.cz"},
        "warnings": [],
    }
    assert log.read_text() == "pod log\n"
    delete = next(command for command in commands if "delete" in command)
    assert delete[3:5] == ["--namespace", "ns"]
    assert delete[5:] == [
        "delete",
        "jobs,pods,services,configmaps,serviceaccounts",
        "-l",
        "coinjoin.run-id=run-1",
        "--ignore-not-found",
        "--wait=false",
    ]
    assert [command for command in commands if command[0] == "qdel"] == [
        ["qdel", "11.pbs-m1.metacentrum.cz"],
        ["qdel", "12.pbs-m1.metacentrum.cz"],
    ]


def test_stop_counts_finished_jobs_as_cancelled_and_reports_real_failures(tmp_path, monkeypatch, capsys) -> None:
    _record(tmp_path, "run-1", "blocksci", "11.pbs-m1.metacentrum.cz")
    _record(tmp_path, "run-1", "unified-report", "12.pbs-m1.metacentrum.cz")

    def run(command, **_kwargs):
        if command[:2] == ["qdel", "11.pbs-m1.metacentrum.cz"]:
            return subprocess.CompletedProcess(command, 35, "", "qdel: Job has finished 11.pbs-m1")
        if command[0] == "qdel":
            return subprocess.CompletedProcess(command, 1, "", "qdel: Unauthorized Request")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/qdel")

    code = stop.main(["--run-id", "run-1", "--pbs-only", "--json"], runs_root=tmp_path)

    assert code != 0
    result = json.loads(capsys.readouterr().out)
    assert result["complete"] is False
    assert result["cancelled"] == {"blocksci": "11.pbs-m1.metacentrum.cz"}
    assert result["warnings"] == [
        "unified-report: qdel 12.pbs-m1.metacentrum.cz failed (exit 1): qdel: Unauthorized Request"
    ]


def test_stop_without_kubectl_is_incomplete(tmp_path, monkeypatch, capsys) -> None:
    def run(command, **_kwargs):
        if command[0] == "kubectl":
            raise FileNotFoundError("kubectl")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    code = stop.main(["--run-id", "run-1", "--json"], runs_root=tmp_path)

    assert code != 0
    assert "kubectl is missing" in json.loads(capsys.readouterr().out)["warnings"][0]
