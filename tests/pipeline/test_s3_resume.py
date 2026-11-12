"""full-run --resume-run: which stages a resume keeps, adopts, cancels and resubmits."""

from unittest import mock

import pytest
from config_support import configuration

from coinjoin_pipeline.execution import s3_resume, s3_submission
from coinjoin_pipeline.execution.s3_submission import ResumedGraph, S3StageRunner, s3_full_run_plan_from_args
from coinjoin_pipeline.execution.stages import s3_full_run_plan
from coinjoin_pipeline.execution.probes import PROBE_QUEUED, PROBE_RUNNING, PROBE_TERMINAL, PROBE_UNKNOWN
from coinjoin_pipeline.storage.s3 import S3Target

ANALYSIS, BLOCKSCI, REPORT = "coinjoin-analysis", "blocksci", "unified-report"
PLAN = s3_full_run_plan(
    analysis_pbs=True,
    blocksci_pbs=True,
    mappings_pbs=False,
    blocksci_workflow="combined",
    blocksci_task="detect",
    unified_report=True,
)


def _args(tmp_path, **overrides):
    values = dict(
        action="full-run",
        resume_run=True,
        artifact_backend="s3",
        driver="kubernetes",
        artifact_uri="s3://bucket/runs",
        run_id="run-1",
        runs_root=tmp_path,
        s3_endpoint_url="https://s3.example",
        s3_credentials_file="/credentials",
        s3_profile="coinjoin",
        s3_secret_name="coinjoin-s3",
        namespace="coinjoin-ns",
        reuse_namespace=True,
        analysis_pbs=True,
        blocksci_pbs=True,
    )
    values.update(overrides)
    return configuration(**values)


def test_the_graph_under_test_is_baseline_and_detection_joined_by_the_report() -> None:
    assert [stage.name for stage in PLAN.scheduled()] == [ANALYSIS, BLOCKSCI, REPORT]
    assert set(PLAN.get(REPORT).dependencies) == {ANALYSIS, BLOCKSCI}


def test_a_finished_run_resubmits_nothing() -> None:
    decision = s3_resume.decide_resume(PLAN, dict.fromkeys((ANALYSIS, BLOCKSCI, REPORT), "done"), {}, {})

    assert decision.completed == {ANALYSIS, BLOCKSCI, REPORT}
    assert (decision.adopted, decision.cancel, decision.resubmit) == ({}, {}, ())


def test_live_jobs_whose_inputs_are_finished_or_live_are_adopted() -> None:
    decision = s3_resume.decide_resume(
        PLAN,
        {ANALYSIS: "done"},
        {ANALYSIS: "1.pbs", BLOCKSCI: "2.pbs", REPORT: "3.pbs"},
        {ANALYSIS: PROBE_TERMINAL, BLOCKSCI: PROBE_RUNNING, REPORT: PROBE_QUEUED},
    )

    assert decision.completed == {ANALYSIS}
    assert decision.adopted == {BLOCKSCI: "2.pbs", REPORT: "3.pbs"}
    assert (decision.cancel, decision.resubmit) == ({}, ())


def test_a_failed_stage_is_resubmitted_and_its_waiting_dependent_replaced() -> None:
    # The queued report still depends on the failed job ID, so PBS would never release it.
    decision = s3_resume.decide_resume(
        PLAN,
        {ANALYSIS: "done", BLOCKSCI: "failed"},
        {ANALYSIS: "1.pbs", BLOCKSCI: "2.pbs", REPORT: "3.pbs"},
        {ANALYSIS: PROBE_TERMINAL, BLOCKSCI: PROBE_TERMINAL, REPORT: PROBE_QUEUED},
    )

    assert decision.completed == {ANALYSIS}
    assert decision.adopted == {}
    assert decision.cancel == {REPORT: "3.pbs"}
    assert decision.resubmit == (BLOCKSCI, REPORT)


def test_a_job_that_ended_without_a_marker_is_resubmitted_and_unrelated_stages_kept() -> None:
    decision = s3_resume.decide_resume(
        PLAN,
        {},
        {ANALYSIS: "1.pbs", BLOCKSCI: "2.pbs"},
        {ANALYSIS: PROBE_RUNNING, BLOCKSCI: PROBE_TERMINAL},
    )

    assert decision.adopted == {ANALYSIS: "1.pbs"}
    assert decision.resubmit == (BLOCKSCI, REPORT)


def test_a_run_that_died_before_submitting_resubmits_the_whole_graph() -> None:
    decision = s3_resume.decide_resume(PLAN, {}, {}, {})

    assert decision.resubmit == (ANALYSIS, BLOCKSCI, REPORT)


def test_the_runner_skips_kept_stages_and_counts_finished_dependencies(tmp_path) -> None:
    args = _args(tmp_path)
    plan = s3_full_run_plan_from_args(args)
    runner = S3StageRunner(args, S3Target.from_args(args), plan, [], {BLOCKSCI: "2.pbs"}, frozenset({ANALYSIS}))
    submitted = []
    with (
        mock.patch.object(S3StageRunner, "_analysis") as analysis,
        mock.patch.object(S3StageRunner, "_blocksci_work") as blocksci,
        mock.patch.object(s3_submission, "render_unified_report_s3_pbs", return_value="#PBS report"),
        mock.patch.object(s3_submission, "blocksci_export_pbs_command", return_value="report"),
        mock.patch.object(s3_submission, "resolve_unified_report_image", return_value="report:tag"),
        mock.patch.object(s3_submission, "resolve_uploader_image", return_value="uploader:tag"),
        mock.patch.object(s3_submission, "pinned_reference", side_effect=lambda image: image),
        mock.patch.object(s3_submission, "clear_s3_stage_markers"),
        mock.patch.object(
            s3_submission,
            "submit_pbs_job",
            side_effect=lambda job, _dry: submitted.append((job.stage, job.dependencies)) or "3.pbs",
        ),
    ):
        runner.submit_all()

    analysis.assert_not_called()
    blocksci.assert_not_called()
    # The finished baseline is no dependency; the adopted BlockSci job is.
    assert submitted == [(REPORT, ("2.pbs",))]
    assert runner.jobs == {BLOCKSCI: "2.pbs", REPORT: "3.pbs"}
    assert (tmp_path / "run-1" / ".pbs" / "unified-report.jobid").read_text() == "3.pbs\n"


@pytest.fixture
def remote(tmp_path):
    """The run's records: upload marker, stage markers, job IDs and probe states."""
    state = {
        "upload": "done",
        "markers": {ANALYSIS: "done", BLOCKSCI: "failed"},
        "probes": {"1.pbs": PROBE_TERMINAL, "2.pbs": PROBE_TERMINAL, "3.pbs": PROBE_QUEUED},
        "job": {"status": {"active": 1}},
    }
    pbs = tmp_path / "run-1" / ".pbs"
    pbs.mkdir(parents=True)
    for stage, job_id in ((ANALYSIS, "1.pbs"), (BLOCKSCI, "2.pbs"), (REPORT, "3.pbs")):
        (pbs / f"{stage}.jobid").write_text(job_id + "\n")

    def marker(_access, done_uri, _failed_uri):
        if "/.k8s/" in done_uri:
            return state["upload"]
        return state["markers"].get(done_uri.rsplit("/", 1)[1].removesuffix(".done"))

    with (
        mock.patch.object(s3_resume, "s3_access_preflight"),
        mock.patch.object(s3_resume, "require_qsub"),
        mock.patch.object(s3_resume, "marker_state", side_effect=marker),
        mock.patch.object(s3_resume, "pbs_job_probe", side_effect=lambda job_id: lambda: state["probes"][job_id]),
        mock.patch.object(s3_resume, "_emulation_job", side_effect=lambda _args: state["job"]),
        mock.patch.object(s3_resume, "qdel_error", return_value=None) as qdel,
        mock.patch.object(
            s3_resume, "submit_s3_pbs_graph", return_value={BLOCKSCI: "4.pbs", REPORT: "5.pbs"}
        ) as submit,
        mock.patch.object(s3_resume, "wait_for_pbs_graph") as wait,
        mock.patch.object(s3_resume, "wait_for_emulation_upload") as upload_wait,
    ):
        yield state, {"qdel": qdel, "submit": submit, "wait": wait, "upload_wait": upload_wait}


def test_resume_replaces_the_failed_branch_and_waits_for_it(tmp_path, remote) -> None:
    _, calls = remote

    s3_resume.resume_s3_full_run(_args(tmp_path))

    calls["upload_wait"].assert_not_called()
    calls["qdel"].assert_called_once_with("3.pbs")
    calls["submit"].assert_called_once()
    assert calls["submit"].call_args.args[1] == ResumedGraph(frozenset({ANALYSIS}), {})
    assert calls["wait"].call_args.args[2] == {BLOCKSCI: "4.pbs", REPORT: "5.pbs"}


def test_resume_keeps_waiting_for_an_emulation_that_is_still_running(tmp_path, remote) -> None:
    state, calls = remote
    state["upload"] = None

    s3_resume.resume_s3_full_run(_args(tmp_path))

    calls["upload_wait"].assert_called_once()
    calls["submit"].assert_called_once()


@pytest.mark.parametrize(
    ("upload", "job", "message"),
    [
        ("failed", None, "emulation of run run-1 failed"),
        (None, None, "is gone without an upload marker"),
        (None, {"status": {"succeeded": 1}}, r"ended \(done\) without an upload marker"),
    ],
)
def test_resume_refuses_a_run_whose_emulation_left_nothing(tmp_path, remote, upload, job, message) -> None:
    state, calls = remote
    state["upload"], state["job"] = upload, job

    with pytest.raises(RuntimeError, match=message):
        s3_resume.resume_s3_full_run(_args(tmp_path))

    calls["submit"].assert_not_called()


def test_resume_refuses_while_pbs_cannot_confirm_a_job(tmp_path, remote) -> None:
    state, calls = remote
    state["probes"]["2.pbs"] = PROBE_UNKNOWN

    with pytest.raises(RuntimeError, match="cannot confirm the state of blocksci"):
        s3_resume.resume_s3_full_run(_args(tmp_path))

    calls["qdel"].assert_not_called()
    calls["submit"].assert_not_called()


def test_resume_does_not_resubmit_over_a_job_it_could_not_cancel(tmp_path, remote) -> None:
    _, calls = remote
    calls["qdel"].return_value = "qdel 3.pbs failed (exit 1): permission denied"

    with pytest.raises(RuntimeError, match="cannot cancel the stale unified-report job"):
        s3_resume.resume_s3_full_run(_args(tmp_path))

    calls["submit"].assert_not_called()


def test_a_dry_run_resume_cancels_and_waits_for_nothing(tmp_path, remote) -> None:
    _, calls = remote

    s3_resume.resume_s3_full_run(_args(tmp_path, dry_run=True))

    calls["qdel"].assert_not_called()
    calls["wait"].assert_not_called()
    calls["submit"].assert_called_once()
