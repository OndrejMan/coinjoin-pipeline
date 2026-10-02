import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from coinjoin_pipeline.execution.stages import (
    StageKind,
    analysis_plan,
    s3_full_run_plan,
)


def test_shared_plan_declares_independent_analysis_and_report_join() -> None:
    plan = analysis_plan(
        analysis_pbs=True,
        blocksci_pbs=False,
        mappings_pbs=True,
    )
    by_name = {stage.name: stage for stage in plan}

    assert by_name["coinjoin-analysis"].runner == "pbs"
    assert by_name["coinjoin-mappings"].dependencies == ("coinjoin-analysis",)
    assert by_name["blocksci"].dependencies == ()
    assert by_name["unified-report"].dependencies == (
        "coinjoin-analysis",
        "blocksci",
        "coinjoin-mappings",
    )


def test_parallel_plan_declares_join_dependencies() -> None:
    plan = analysis_plan(
        analysis_pbs=False,
        blocksci_pbs=True,
        mappings_pbs=True,
    )
    by_name = {stage.name: stage for stage in plan}
    report = by_name.get("unified-report")

    assert by_name["blocksci"].dependencies == ()
    assert report is not None
    assert report.runner == "pbs"
    assert report.dependencies == (
        "coinjoin-analysis",
        "blocksci",
        "coinjoin-mappings",
    )


def test_s3_reusable_plan_inserts_the_parser_stage() -> None:
    stages = s3_full_run_plan(mappings_pbs=True, blocksci_workflow="reusable")
    by_name = {stage.name: stage for stage in stages}

    assert by_name["coinjoin-analysis"].dependencies == ("kubernetes-emulation",)
    assert by_name["coinjoin-mappings"].dependencies == ("coinjoin-analysis",)
    assert by_name["blocksci-parse"].dependencies == ("kubernetes-emulation",)
    assert by_name["blocksci-analyze"].dependencies == ("blocksci-parse",)
    assert by_name["unified-report"].dependencies == (
        "coinjoin-analysis",
        "blocksci-analyze",
        "coinjoin-mappings",
    )


def test_s3_combined_plan_waits_for_the_emulation_upload() -> None:
    stages = s3_full_run_plan(mappings_pbs=False, blocksci_workflow="combined")
    by_name = {stage.name: stage for stage in stages}

    assert by_name["coinjoin-analysis"].dependencies == ("kubernetes-emulation",)
    assert by_name["blocksci"].dependencies == ("kubernetes-emulation",)
    assert by_name["unified-report"].dependencies == (
        "coinjoin-analysis",
        "blocksci",
    )


def test_s3_plan_skips_stages_this_invocation_does_not_submit() -> None:
    stages = s3_full_run_plan(
        mappings_pbs=True,
        blocksci_workflow="combined",
        analysis_pbs=False,
        blocksci_pbs=True,
    )
    by_name = {stage.name: stage for stage in stages}

    assert "coinjoin-analysis" not in by_name
    # Without a baseline job to wait for, mappings only needs the emulation
    # bundle that a previous run published under the same prefix.
    assert by_name["coinjoin-mappings"].dependencies == ("kubernetes-emulation",)
    assert by_name["unified-report"].dependencies == ("blocksci", "coinjoin-mappings")


def test_s3_resumed_blocksci_joins_existing_baseline_in_report_stage() -> None:
    stages = s3_full_run_plan(
        mappings_pbs=False,
        blocksci_workflow="combined",
        analysis_pbs=False,
        blocksci_pbs=True,
    )

    assert [stage.name for stage in stages] == [
        "kubernetes-emulation",
        "blocksci",
        "unified-report",
    ]
    assert stages.get("unified-report").dependencies == ("blocksci",)


def test_s3_plan_stops_after_the_parser_for_a_parse_task() -> None:
    stages = s3_full_run_plan(
        mappings_pbs=False,
        blocksci_workflow="reusable",
        analysis_pbs=False,
        blocksci_task="parse",
    )

    assert [stage.name for stage in stages] == [
        "kubernetes-emulation",
        "blocksci-parse",
    ]


def test_scheduled_stages_exclude_the_cluster_emulation() -> None:
    stages = s3_full_run_plan(mappings_pbs=True, blocksci_workflow="reusable")

    assert [stage.name for stage in stages.scheduled()] == [
        "coinjoin-analysis",
        "blocksci-parse",
        "blocksci-analyze",
        "coinjoin-mappings",
        "unified-report",
    ]


def test_dependents_of_a_stage_are_transitive_and_ordered() -> None:
    stages = s3_full_run_plan(mappings_pbs=True, blocksci_workflow="reusable")

    # The parser blocks the analyzer, which in turn blocks the report.
    assert [stage.name for stage in stages.dependents_of("blocksci-parse")] == [
        "blocksci-analyze",
        "unified-report",
    ]
    assert [stage.name for stage in stages.dependents_of("coinjoin-analysis")] == [
        "coinjoin-mappings",
        "unified-report",
    ]
    # A failed baseline never invalidates the independent BlockSci branch.
    assert "blocksci-analyze" not in {stage.name for stage in stages.dependents_of("coinjoin-analysis")}


def test_dependency_ids_follow_the_declared_edges() -> None:
    stages = s3_full_run_plan(mappings_pbs=True, blocksci_workflow="reusable")
    jobs = {
        "coinjoin-analysis": "analysis.job",
        "blocksci-parse": "parse.job",
        "blocksci-analyze": "analyze.job",
        "coinjoin-mappings": "mappings.job",
    }

    assert stages.dependency_id("blocksci-analyze", jobs) == "parse.job"
    assert stages.dependency_id("coinjoin-mappings", jobs) == "analysis.job"
    assert stages.dependency_ids("unified-report", jobs) == (
        "analysis.job",
        "analyze.job",
        "mappings.job",
    )
    # Stages whose upstream was not submitted carry no scheduler dependency.
    assert stages.dependency_id("coinjoin-analysis", jobs) is None


def test_graph_orders_dependencies_and_rejects_cycles():
    import pytest

    from coinjoin_pipeline.execution.stages import StageGraph, StagePlan

    first = StagePlan("first", StageKind.BASELINE, "local")
    last = StagePlan("last", StageKind.REPORT, "local", ("first",))
    assert [stage.name for stage in StageGraph((last, first))] == ["first", "last"]
    with pytest.raises(ValueError, match="cycle"):
        StageGraph((StagePlan("first", StageKind.BASELINE, "local", ("last",)), last))
