"""The run layout clients generate from must name what the pipeline really writes."""

import json

from coinjoin_pipeline import run_layout
from coinjoin_pipeline.execution.mainnet_sync import SYNC_STAGES
from coinjoin_pipeline.execution.stages import s3_full_run_plan
from coinjoin_pipeline.storage.s3 import is_valid_run_id, stage_marker_uri, upload_marker_uri


def test_every_stage_of_the_layout_is_a_real_stage() -> None:
    plan = s3_full_run_plan(
        analysis_pbs=True,
        blocksci_pbs=True,
        mappings_pbs=False,
        blocksci_workflow="combined",
        blocksci_task="detect",
        unified_report=True,
    )
    stages = {stage.name for stage in plan.stages} | {f"mainnet-{stage}" for stage in SYNC_STAGES}
    layout = run_layout.run_layout()

    assert {group["stage"] for group in layout["artifacts"].values()} <= stages


def test_the_markers_are_the_directories_the_marker_uris_use() -> None:
    layout = run_layout.run_layout()

    assert stage_marker_uri("p", "blocksci", "done").split("/")[1] in layout["markers"]
    assert upload_marker_uri("p", "done").split("/")[1] in layout["markers"]


def test_the_run_id_rule_matches_the_python_validation() -> None:
    import re

    layout = run_layout.run_layout()["runId"]
    pattern = re.compile(layout["pattern"])
    for run_id in ("run-1", "2026-11-11_10-00_fin", "a" * 63):
        assert is_valid_run_id(run_id) and pattern.fullmatch(run_id) and len(run_id) <= layout["maxLength"]
    assert not pattern.fullmatch("-starts-with-dash")


def test_the_module_prints_the_layout_as_json(capsys) -> None:
    assert run_layout.main() == 0
    assert json.loads(capsys.readouterr().out) == run_layout.run_layout()
