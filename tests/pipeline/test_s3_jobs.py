"""Scheduler wiring through the single job submission boundary, without PBS or S3."""

import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from exporters.worker import parse_args as parse_worker_args

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.context import RunContext
from coinjoin_pipeline.execution import locks, s3_markers, s3_staging, s3_submission
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
        fail_upload=None,
    )
    for name in (
        "render_coinjoin_analysis_s3_pbs",
        "render_mappings_s3_pbs",
        "render_blocksci_s3_pbs",
        "render_blocksci_parse_s3_pbs",
        "render_blocksci_update_s3_pbs",
        "render_blocksci_analyze_s3_pbs",
        "render_unified_report_s3_pbs",
        "render_coinjoin_analysis_dumplings_s3_pbs",
        "render_blocksci_index_backup_s3_pbs",
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
        lambda access, prefix, stage: state.events.append(("clear", stage)),
    )

    def upload(access, prefix, stage, record):
        if stage == state.fail_upload:
            raise ArtifactTransportError("S3 refused the job ID")
        state.events.append(("upload", stage, prefix, record.read_text()))

    monkeypatch.setattr(s3_submission, "upload_s3_job_id", upload)
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

    def config(overrides=None):
        values = {
            "action": "pbs-from-s3",
            "run_id": "run-1",
            "runs_root": str(tmp_path),
            "artifacts.uri": "s3://bucket/runs",
            "artifacts.endpoint_url": "https://s3.example",
            "artifacts.credentials_file": "/tmp/credentials",
            "artifacts.profile": "test",
            "stages.analysis": True,
            "stages.blocksci": True,
        }
        values.update(overrides or {})
        return RunContext.prepare(PipelineConfiguration.from_paths(values), "test").config

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
    # Each job ID reaches S3 right after its submission, from the local record.
    assert backend.events == [
        event
        for number, stage in enumerate(("coinjoin-analysis", "blocksci", "unified-report"), 1)
        for event in (
            ("clear", stage),
            ("submit", stage),
            ("upload", stage, "s3://bucket/runs/run-1", f"{number}.server\n"),
        )
    ]
    marker = Path(config.runs_root) / config.run_id / ".pbs/unified-report.jobid"
    assert marker.read_text() == "3.server\n"


def test_failed_submission_rolls_back_every_recorded_job(backend):
    backend.fail_stage = "unified-report"
    with pytest.raises(RuntimeError, match="refused"):
        s3_submission.submit_s3_pbs_graph(backend.config())
    assert set(backend.cancelled) == {"1.server", "2.server"}


def test_a_job_id_that_cannot_reach_s3_rolls_back_the_submission(backend):
    backend.fail_upload = "blocksci"
    with pytest.raises(ArtifactTransportError, match="refused the job ID"):
        s3_submission.submit_s3_pbs_graph(backend.config())
    # The job whose ID S3 refused is cancelled too: no other frontend could find it.
    assert set(backend.cancelled) == {"1.server", "2.server"}
    assert [job.stage for job in backend.jobs] == ["coinjoin-analysis", "blocksci"]


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
    config = backend.config({"dry_run": True})
    s3_submission.submit_s3_pbs_graph(config)
    assert backend.jobs == []
    assert backend.staged == []
    assert all(event == "render" for event, stage in backend.events)
    assert not (Path(config.runs_root) / config.run_id).exists()


def test_mapping_fee_options_reach_the_s3_mappings_job(backend):
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            {"stages.mappings": True, "mappings.coordination_fee_rate": 0.01, "mappings.max_decomposition_fee": 1234}
        )
    )
    rendered = backend.rendered["render_mappings_s3_pbs"]
    assert rendered["settings"].coordination_fee_rate == 0.01
    assert rendered["settings"].max_decomposition_fee == 1234


def test_mappings_wait_for_baseline_and_gate_the_report(backend):
    s3_submission.submit_s3_pbs_graph(backend.config({"stages.mappings": True}))
    by_name = {job.stage: job for job in backend.jobs}
    ids = {job.stage: f"{index}.server" for index, job in enumerate(backend.jobs, 1)}
    assert by_name["coinjoin-mappings"].dependencies == (ids["coinjoin-analysis"],)
    assert set(by_name["unified-report"].dependencies) == {
        ids[name] for name in ("coinjoin-analysis", "blocksci", "coinjoin-mappings")
    }


def test_resume_requires_existing_baseline(backend):
    backend.exists = False
    with pytest.raises(ArtifactTransportError, match="existing"):
        s3_submission.submit_s3_pbs_graph(backend.config({"stages.analysis": False}))
    assert backend.jobs == []


def test_blocksci_resume_still_has_a_report_stage(backend):
    s3_submission.submit_s3_pbs_graph(backend.config({"stages.analysis": False}))
    assert [job.stage for job in backend.jobs] == ["blocksci", "unified-report"]
    assert backend.jobs[-1].dependencies == ("1.server",)
    assert backend.rendered["render_blocksci_s3_pbs"]["include_report"] is False
    assert backend.rendered["render_blocksci_s3_pbs"]["export_analysis"] is True


def test_reusable_chain_uses_parser_analysis_and_report_dependencies(backend):
    s3_submission.submit_s3_pbs_graph(backend.config({"blocksci.workflow": "reusable"}))
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
        backend.config({"stages.analysis": False, "blocksci.workflow": "reusable", "blocksci.task": "parse"})
    )
    assert [job.stage for job in backend.jobs] == ["blocksci-parse"]
    assert backend.staged == ["run-1"]


@pytest.mark.parametrize("task,workflow", [("parse", "reusable"), ("update", "cached")])
def test_archive_checkpoint_reaches_parse_and_update_renderer(backend, task, workflow):
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            {
                "stages.analysis": False,
                "blocksci.workflow": workflow,
                "blocksci.task": task,
                "blocksci.bitcoin_blocks_uri": "s3://bucket/mainnet",
                "blocksci.network": "bitcoin",
                "blocksci.max_block": 12,
                "blocksci.expected_block_hash": "a" * 64,
                "blocksci.cache_source_run_id": "previous" if task == "update" else None,
            }
        )
    )
    values = backend.rendered[f"render_blocksci_{task}_s3_pbs"]
    assert values["blocksci"].expected_block_hash == "a" * 64
    assert values["blocksci"].max_block == 12


def test_cached_notebook_has_no_report_or_worker_staging(backend):
    s3_submission.submit_s3_pbs_graph(
        backend.config({"stages.analysis": False, "blocksci.workflow": "cached", "blocksci.task": "notebook"})
    )
    assert [job.stage for job in backend.jobs] == ["blocksci-notebook"]
    assert backend.staged == []


def test_notebook_refuses_a_source_runs_loose_index_without_an_archive(backend, monkeypatch):
    monkeypatch.setattr(
        s3_submission,
        "s3_object_exists",
        lambda access, uri: uri.endswith(("/blocksci_data/config.json", "/blocksci_data/parsed/chain/block.dat")),
    )
    with pytest.raises(ArtifactTransportError, match="source BlockSci cache manifest"):
        s3_submission.submit_s3_pbs_graph(
            backend.config(
                {
                    "stages.analysis": False,
                    "blocksci.workflow": "cached",
                    "blocksci.task": "notebook",
                    "blocksci.cache_source_run_id": "source",
                }
            )
        )
    assert backend.jobs == []


def test_resource_and_image_overrides_reach_the_actual_job_renderers(backend):
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            {
                "pbs.ncpus": 6,
                "pbs.blocksci.ncpus": 12,
                "pbs.unified_report.ncpus": 2,
                "pbs.blocksci_image": "docker://blocksci:chosen",
            }
        )
    )
    assert backend.rendered["render_coinjoin_analysis_s3_pbs"]["resources"].ncpus == 6
    assert backend.rendered["render_blocksci_s3_pbs"]["resources"].ncpus == 12
    assert backend.rendered["render_blocksci_s3_pbs"]["image"] == "docker://blocksci:chosen"
    assert backend.rendered["render_unified_report_s3_pbs"]["resources"].ncpus == 2


@pytest.mark.parametrize("workflow", ["combined", "reusable", "cached"])
def test_detector_overrides_reach_both_analysis_and_report_workers(backend, workflow):
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            {
                "engine": "joinmarket",
                "blocksci.workflow": workflow,
                "min_input_count": 7,
                "joinmarket.detector": "possible",
                "joinmarket.min_base_fee": 4321,
                "joinmarket.percentage_fee": 0.00013,
                "joinmarket.max_depth": 789,
            }
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
    s3_submission.submit_s3_pbs_graph(backend.config({"stages.blocksci": False, "stage_exporters": True}))
    assert backend.staged == ["run-1"]
    assert [job.stage for job in backend.jobs] == ["coinjoin-analysis"]


def test_read_only_analysis_passes_its_source_cache_to_the_renderer(backend, tmp_path):
    script = tmp_path / "scan.py"
    script.write_text("print('scan')\n")
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            {
                "stages.analysis": False,
                "blocksci.workflow": "cached",
                "blocksci.task": "script",
                "blocksci.script": str(script),
                "blocksci.cache_source_run_id": "mainnet-index",
            }
        )
    )
    assert [job.stage for job in backend.jobs] == ["blocksci-script"]
    assert backend.rendered["render_blocksci_analyze_s3_pbs"]["blocksci"].cache_source_run_id == "mainnet-index"


def test_read_only_analysis_needs_a_published_source_cache(backend, tmp_path):
    script = tmp_path / "scan.py"
    script.write_text("print('scan')\n")
    backend.exists = False
    with pytest.raises(ArtifactTransportError, match="mainnet-index"):
        s3_submission.submit_s3_pbs_graph(
            backend.config(
                {
                    "stages.analysis": False,
                    "blocksci.workflow": "cached",
                    "blocksci.task": "script",
                    "blocksci.script": str(script),
                    "blocksci.cache_source_run_id": "mainnet-index",
                }
            )
        )
    assert backend.jobs == []


def test_persistent_update_needs_no_source_run_and_reaches_the_renderer(backend):
    backend.exists = False  # no S3 cache anywhere: the index lives on the multijob scratch
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            {
                "stages.analysis": False,
                "blocksci.workflow": "cached",
                "blocksci.task": "update",
                "blocksci.index_dir": "/scratch/multijob-brno/user/blocksci-mainnet",
                "blocksci.index_backup_uri": "s3://bucket/blocksci-index",
                "pbs.blocksci.multijob": "brno",
                "blocksci.bitcoin_blocks_uri": "s3://bucket/mainnet",
                "blocksci.network": "bitcoin",
                "blocksci.max_block": 850100,
                "blocksci.expected_block_hash": "a" * 64,
            }
        )
    )
    values = backend.rendered["render_blocksci_update_s3_pbs"]
    assert values["blocksci"].cache_source_run_id is None
    assert values["multijob"] == "brno"
    index = values["blocksci"]
    assert (index.index_dir, index.index_backup_uri) == (
        "/scratch/multijob-brno/user/blocksci-mainnet",
        "s3://bucket/blocksci-index",
    )


def test_the_backup_task_submits_only_the_backup_job(backend):
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            {
                "stages.analysis": False,
                "blocksci.workflow": "cached",
                "blocksci.task": "backup",
                "blocksci.index_dir": "/scratch/multijob-brno/user/blocksci-mainnet",
                "blocksci.index_backup_uri": "s3://bucket/blocksci-index",
                "pbs.blocksci.multijob": "brno",
            }
        )
    )
    assert [job.stage for job in backend.jobs] == ["blocksci-backup"]
    values = backend.rendered["render_blocksci_index_backup_s3_pbs"]
    assert values["blocksci"].index_backup_uri == "s3://bucket/blocksci-index"
    assert values["multijob"] == "brno"
    assert backend.staged == []


def test_index_jobs_at_a_fixed_state_prefix_clear_their_markers_there(backend, monkeypatch):
    prefixes = []
    monkeypatch.setattr(
        s3_submission, "clear_s3_stage_markers", lambda access, prefix, stage: prefixes.append((prefix, stage))
    )
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            {
                "stages.analysis": False,
                "blocksci.workflow": "cached",
                "blocksci.task": "backup",
                "blocksci.index_dir": "/scratch/multijob-brno/user/blocksci-mainnet",
                "blocksci.index_backup_uri": "s3://bucket/blocksci-index",
                "blocksci.index_state_uri": "s3://bucket/bitcoin-mainnet/index",
                "pbs.blocksci.multijob": "brno",
            }
        )
    )
    assert prefixes == [("s3://bucket/bitcoin-mainnet/index", "blocksci-backup")]
    target = backend.rendered["render_blocksci_index_backup_s3_pbs"]["target"]
    assert target.run_prefix == "s3://bucket/bitcoin-mainnet/index"


def test_a_fixed_state_prefix_always_refreshes_its_exporters(backend, monkeypatch):
    uploads = []
    monkeypatch.setattr(s3_staging, "upload_exporters", lambda access, prefix, root: uploads.append(prefix))
    monkeypatch.setattr(
        s3_staging, "staged_exporters_state", lambda *_: pytest.fail("a fixed prefix never reuses staged exporters")
    )
    config = backend.config(
        {
            "stages.analysis": False,
            "blocksci.workflow": "cached",
            "blocksci.task": "update",
            "blocksci.index_dir": "/scratch/multijob-brno/user/blocksci-mainnet",
            "blocksci.index_state_uri": "s3://bucket/bitcoin-mainnet/index",
            "pbs.blocksci.multijob": "brno",
            "blocksci.bitcoin_blocks_uri": "s3://bucket/mainnet",
            "blocksci.network": "bitcoin",
            "blocksci.max_block": 850100,
            "blocksci.expected_block_hash": "a" * 64,
        }
    )
    s3_staging.ensure_staged_exporters(config)
    s3_staging.ensure_staged_exporters(config)
    assert uploads == ["s3://bucket/bitcoin-mainnet/index", "s3://bucket/bitcoin-mainnet/index"]


def test_staging_drops_exporters_the_checkout_no_longer_has(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(s3_staging, "ensure_local_exporters", lambda _root: None)
    monkeypatch.setattr(s3_staging, "s3_object_exists", lambda *_: True)
    monkeypatch.setattr(
        s3_staging, "run_s5cmd", lambda _access, *args: calls.append(args) or SimpleNamespace(returncode=0, stderr="")
    )
    s3_staging.upload_exporters(SimpleNamespace(), "s3://bucket/runs/run-1", tmp_path)
    assert calls[0][:2] == ("sync", "--delete")


def test_a_dumplings_uri_turns_the_baseline_into_the_mainnet_one(backend):
    s3_submission.submit_s3_pbs_graph(
        backend.config(
            {"stages.blocksci": False, "coinjoin_type": "joinmarket", "dumplings_uri": "s3://bucket/dumplings"}
        )
    )
    assert [job.stage for job in backend.jobs] == ["coinjoin-analysis"]
    values = backend.rendered["render_coinjoin_analysis_dumplings_s3_pbs"]
    assert values["dumplings_uri"] == "s3://bucket/dumplings"
    assert values["coinjoin_type"] == "joinmarket"
    assert "render_coinjoin_analysis_s3_pbs" not in backend.rendered
