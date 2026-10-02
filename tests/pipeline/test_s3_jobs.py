"""Scheduler wiring through the single job submission boundary, without PBS or S3."""

import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from exporters.worker import parse_args as parse_worker_args

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.context import RunContext
from coinjoin_pipeline.execution import locks, s3_markers, s3_submission
from coinjoin_pipeline.execution.pbs.submission import PBSJobSpec, submit_pbs_job
from coinjoin_pipeline.storage.s3 import ArtifactTransportError


@pytest.fixture
def backend(monkeypatch, tmp_path):
    state = SimpleNamespace(
        jobs=[],
        events=[],
        rendered={},
        cancelled=[],
        staged=[],
        exists=True,
        fail_stage=None,
    )
    for name in (
        "render_coinjoin_analysis_s3_pbs",
        "render_mappings_s3_pbs",
        "render_blocksci_s3_pbs",
        "render_blocksci_parse_s3_pbs",
        "render_blocksci_update_s3_pbs",
        "render_blocksci_analyze_s3_pbs",
        "render_unified_report_s3_pbs",
    ):

        def render(*, _name=name, **values):
            state.rendered[_name] = values
            return _name

        monkeypatch.setattr(s3_submission, name, render)

    def submit(job, dry_run):
        if dry_run:
            state.events.append(("render", job.stage))
            return None
        if job.stage == state.fail_stage:
            raise RuntimeError("scheduler refused job")
        state.events.append(("submit", job.stage))
        state.jobs.append(job)
        return f"{len(state.jobs)}.server"

    monkeypatch.setattr(s3_submission, "submit_pbs_job", submit)
    monkeypatch.setattr(
        s3_submission,
        "clear_s3_stage_markers",
        lambda access, uri, run, stage: state.events.append(("clear", stage)),
    )
    monkeypatch.setattr(
        s3_submission,
        "ensure_staged_exporters",
        lambda config: state.staged.append(config.run_id),
    )
    monkeypatch.setattr(s3_submission, "s3_access_preflight", lambda *args: None)
    monkeypatch.setattr(s3_submission, "s3_object_exists", lambda *args: state.exists)
    monkeypatch.setattr(s3_submission, "ensure_empty_run_prefix", lambda *args: None)
    monkeypatch.setattr(s3_markers, "qdel_pbs_job", lambda job: state.cancelled.append(job) or True)
    monkeypatch.setattr(locks, "pbs_job_probe", lambda job: lambda: "terminal")

    def config(**overrides):
        values = dict(
            action="pbs-from-s3",
            run_id="run-1",
            runs_root=str(tmp_path),
            artifact_uri="s3://bucket/runs",
            s3_endpoint_url="https://s3.example",
            s3_credentials_file="/tmp/credentials",
            s3_profile="test",
            analysisPbs=True,
            blocksciPbs=True,
        )
        values.update(overrides)
        return RunContext.prepare(PipelineConfiguration.from_flat(values), "test").config

    state.config = config
    yield state
    locks.close_locks()


def test_independent_analyzers_then_report_with_persisted_jobs(backend):
    config = backend.config()
    s3_submission.submit_s3_pbs_graph(config)
    assert [job.stage for job in backend.jobs] == [
        "coinjoin-analysis",
        "blocksci",
        "unified-report",
    ]
    assert backend.jobs[0].dependencies == backend.jobs[1].dependencies == ()
    assert backend.jobs[2].dependencies == ("1.server", "2.server")
    assert backend.events == [
        (event, stage) for stage in ("coinjoin-analysis", "blocksci", "unified-report") for event in ("clear", "submit")
    ]
    marker = Path(config.runs_root) / config.run_id / ".pbs/unified-report.jobid"
    assert marker.read_text() == "3.server\n"


def test_failed_submission_rolls_back_every_recorded_job(backend):
    backend.fail_stage = "unified-report"
    with pytest.raises(RuntimeError, match="refused"):
        s3_submission.submit_s3_pbs_graph(backend.config())
    assert set(backend.cancelled) == {"1.server", "2.server"}


def test_overlap_is_rejected_before_new_submission(backend, monkeypatch):
    config = backend.config()
    path = Path(config.runs_root) / config.run_id / ".pbs/blocksci.jobid"
    path.parent.mkdir(parents=True)
    path.write_text("old.server")
    monkeypatch.setattr(locks, "pbs_job_probe", lambda job: lambda: "running")
    with pytest.raises(RuntimeError, match="active|running"):
        s3_submission.submit_s3_pbs_graph(config)
    assert backend.jobs == []


def test_dry_run_has_no_remote_writes_or_local_locks(backend):
    config = backend.config(dry_run=True)
    s3_submission.submit_s3_pbs_graph(config)
    assert backend.jobs == []
    assert backend.staged == []
    assert all(event == "render" for event, stage in backend.events)
    assert not (Path(config.runs_root) / config.run_id).exists()


def test_mapping_fee_options_reach_the_s3_mappings_job(backend):
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            mappingsPbs=True,
            mapping_coordination_fee_rate=0.01,
            mapping_max_decomposition_fee=1234,
        )
    )
    rendered = backend.rendered["render_mappings_s3_pbs"]
    assert rendered["coordination_fee_rate"] == 0.01
    assert rendered["max_decomposition_fee"] == 1234


def test_mappings_wait_for_baseline_and_gate_the_report(backend):
    s3_submission.submit_s3_pbs_graph(backend.config(mappingsPbs=True))
    by_name = {job.stage: job for job in backend.jobs}
    ids = {job.stage: f"{index}.server" for index, job in enumerate(backend.jobs, 1)}
    assert by_name["coinjoin-mappings"].dependencies == (ids["coinjoin-analysis"],)
    assert set(by_name["unified-report"].dependencies) == {
        ids[name] for name in ("coinjoin-analysis", "blocksci", "coinjoin-mappings")
    }


def test_resume_requires_existing_baseline(backend):
    backend.exists = False
    with pytest.raises(ArtifactTransportError, match="existing"):
        s3_submission.submit_s3_pbs_graph(backend.config(analysisPbs=False))
    assert backend.jobs == []


def test_blocksci_resume_still_has_a_report_stage(backend):
    s3_submission.submit_s3_pbs_graph(backend.config(analysisPbs=False))
    assert [job.stage for job in backend.jobs] == ["blocksci", "unified-report"]
    assert backend.jobs[-1].dependencies == ("1.server",)
    assert backend.rendered["render_blocksci_s3_pbs"]["include_report"] is False
    assert backend.rendered["render_blocksci_s3_pbs"]["export_analysis"] is True


def test_reusable_chain_uses_parser_analysis_and_report_dependencies(backend):
    s3_submission.submit_s3_pbs_graph(backend.config(blocksci_workflow="reusable"))
    assert [job.stage for job in backend.jobs] == [
        "coinjoin-analysis",
        "blocksci-parse",
        "blocksci-analyze",
        "unified-report",
    ]
    assert backend.jobs[2].dependencies == ("2.server",)
    assert backend.jobs[3].dependencies == ("1.server", "3.server")


def test_parse_only_stages_the_worker_and_publishes_only_cache(backend):
    s3_submission.submit_s3_pbs_graph(
        backend.config(analysisPbs=False, blocksci_workflow="reusable", blocksci_task="parse")
    )
    assert [job.stage for job in backend.jobs] == ["blocksci-parse"]
    assert backend.staged == ["run-1"]


def test_cached_notebook_has_no_report_or_worker_staging(backend):
    s3_submission.submit_s3_pbs_graph(
        backend.config(analysisPbs=False, blocksci_workflow="cached", blocksci_task="notebook")
    )
    assert [job.stage for job in backend.jobs] == ["blocksci-notebook"]
    assert backend.staged == []


def test_resource_and_image_overrides_reach_the_actual_job_renderers(backend):
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            pbs_ncpus=6,
            pbs_blocksci_ncpus=12,
            pbs_unified_report_ncpus=2,
            pbs_blocksci_image="docker://blocksci:chosen",
        )
    )
    assert backend.rendered["render_coinjoin_analysis_s3_pbs"]["ncpus"] == 6
    assert backend.rendered["render_blocksci_s3_pbs"]["ncpus"] == 12
    assert backend.rendered["render_blocksci_s3_pbs"]["image"] == "docker://blocksci:chosen"
    assert backend.rendered["render_unified_report_s3_pbs"]["ncpus"] == 2


@pytest.mark.parametrize("workflow", ["combined", "reusable", "cached"])
def test_detector_overrides_reach_both_analysis_and_report_workers(backend, workflow):
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            engine="joinmarket",
            blocksci_workflow=workflow,
            min_input_count=7,
            joinmarket_detector="possible",
            joinmarket_min_base_fee=4321,
            joinmarket_percentage_fee=0.00013,
            joinmarket_max_depth=789,
        )
    )
    analyzer = "render_blocksci_s3_pbs" if workflow == "combined" else "render_blocksci_analyze_s3_pbs"
    for renderer, phase in (
        (analyzer, "run" if workflow == "combined" else "analyze"),
        ("render_unified_report_s3_pbs", "report"),
    ):
        command = shlex.split(backend.rendered[renderer]["command"])
        args = parse_worker_args(command[command.index("/mnt/exporters/worker.py") + 1 :])
        assert args.phase == phase
        assert args.run_dir.name == "run-1"
        assert args.coinjoin_type == "joinmarket"
        assert args.min_input_count == 7
        assert args.joinmarket_detector == "possible"
        assert args.joinmarket_min_base_fee == 4321
        assert args.joinmarket_percentage_fee == 0.00013
        assert args.joinmarket_max_depth == 789


def test_scheduler_receives_script_on_stdin_and_both_dependencies():
    from coinjoin_pipeline.execution.pbs import submission

    with (
        mock.patch.object(submission, "require_qsub"),
        mock.patch.object(
            submission.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="123.server\n", stderr=""),
        ) as run,
    ):
        assert submit_pbs_job(PBSJobSpec("report", "#!/bin/bash\nexit 0\n", ("1.server", "2.server"))) == "123.server"
    assert run.call_args.args[0] == ["qsub", "-W", "depend=afterok:1.server:2.server"]
    assert run.call_args.kwargs["input"].startswith("#!/bin/bash")


def test_explicit_staging_prepares_workers_for_a_later_invocation(backend):
    s3_submission.submit_s3_pbs_graph(backend.config(blocksciPbs=False, stage_exporters=True))
    assert backend.staged == ["run-1"]
    assert [job.stage for job in backend.jobs] == ["coinjoin-analysis"]
