from dataclasses import replace

import pytest

from coinjoin_pipeline.configuration import PipelineConfiguration, StageConfiguration
from coinjoin_pipeline.execution import containers, shared_storage_pbs, workflow
from coinjoin_pipeline.execution.local import run_analysis


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("analysis_pbs,blocksci_pbs", [(False, False), (True, False), (False, True), (True, True)])
def test_same_workloads_for_serial_parallel_and_mixed_backends(
    monkeypatch, tmp_path, parallel, analysis_pbs, blocksci_pbs
):
    events = []
    config = replace(
        PipelineConfiguration(),
        parallel=parallel,
        runs_root=str(tmp_path),
        stages=StageConfiguration(analysis=analysis_pbs, blocksci=blocksci_pbs),
    )
    run = tmp_path / "run-a"
    run.mkdir()

    def local_blocksci(config, directory, *, include_report):
        assert not include_report
        events.append("blocksci")

    def local_report(config):
        assert str(run) == config.run_dir
        assert "analysis" in events and "blocksci" in events
        events.append("report")

    def pbs(stage):
        def submit(config, directory, *, wait, **options):
            assert not wait
            if stage == "blocksci":
                assert options == {"include_report": False}
            if stage == "report":
                assert "analysis" in events and "blocksci" in events
            events.append(stage)

        return submit

    monkeypatch.setattr(containers, "run_coinjoin_analysis_docker_stage", lambda *args: events.append("analysis"))
    monkeypatch.setattr(containers, "run_blocksci_docker_stage", local_blocksci)
    monkeypatch.setattr(containers, "run_export_only", local_report)
    monkeypatch.setattr(shared_storage_pbs, "run_coinjoin_analysis_stage", pbs("analysis"))
    monkeypatch.setattr(shared_storage_pbs, "run_blocksci_stage", pbs("blocksci"))
    monkeypatch.setattr(shared_storage_pbs, "run_blocksci_export_stage", pbs("report"))
    monkeypatch.setattr(workflow, "wait_for_pbs_marker", lambda *args, **kwargs: None)
    run_analysis(config, run, tmp_path)
    assert sorted(events) == ["analysis", "blocksci", "report"]
    assert events[-1] == "report"
