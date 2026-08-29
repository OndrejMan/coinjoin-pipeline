import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

from client.stages import analysis_plan, s3_full_run_plan


def test_serial_plan_keeps_blocksci_as_the_report_producer() -> None:
    plan = analysis_plan(
        analysis_pbs=True,
        blocksci_pbs=False,
        mappings_pbs=True,
        parallel=False,
    )

    assert plan.baseline.runner == "pbs"
    assert plan.mappings is not None
    assert plan.mappings.dependencies == ("coinjoin-analysis",)
    assert plan.blocksci.dependencies == ("coinjoin-analysis", "coinjoin-mappings")
    assert plan.blocksci.produces_report is True
    assert plan.report is None


def test_parallel_plan_declares_join_dependencies() -> None:
    plan = analysis_plan(
        analysis_pbs=False,
        blocksci_pbs=True,
        mappings_pbs=True,
        parallel=True,
    )

    assert plan.blocksci.dependencies == ()
    assert plan.blocksci.produces_report is False
    assert plan.report is not None
    assert plan.report.runner == "pbs"
    assert plan.report.dependencies == (
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
