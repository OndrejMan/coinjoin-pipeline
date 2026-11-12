"""Container payload tests use a fake process boundary; no parser or image runs."""

from unittest import mock

from exporters import worker


def test_parse_uses_exclusive_height_of_exported_run_tip(tmp_path):
    run = tmp_path / "run-a"
    blocks = run / "coinjoin_emulator_data/data/btc-node"
    blocks.mkdir(parents=True)
    for height in (0, 4, 9):
        (blocks / f"block_{height}.json").write_text("{}")
    args = worker.parse_args(["parse", "--run-dir", str(run)])
    with mock.patch.object(worker.subprocess, "run") as process:
        worker.execute(args)
    assert process.call_args_list[0].args[0][-2:] == ["--max-block", "10"]
    assert process.call_args_list[1].args[0][-1] == "update"


def test_external_resume_keeps_existing_parser_configuration(tmp_path):
    run = tmp_path / "run-a"
    config = run / "blocksci_data/config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}")
    args = worker.parse_args(["parse", "--run-dir", str(run), "--mode", "external", "--network", "bitcoin"])
    with mock.patch.object(worker.subprocess, "run") as process:
        worker.execute(args)
    assert process.call_count == 1
    assert process.call_args.args[0] == ["blocksci_parser", str(config), "update"]


def test_report_phase_does_not_parse_or_analyze(tmp_path):
    from exporters import cli
    from exporters.blocksci_export import analysis

    args = worker.parse_args(["report", "--run-dir", str(tmp_path / "run-a")])
    with (
        mock.patch.object(worker.subprocess, "run") as process,
        mock.patch.object(analysis, "write_analysis") as analyze,
        mock.patch.object(cli, "assemble_report") as report,
    ):
        worker.execute(args)
    process.assert_not_called()
    analyze.assert_not_called()
    report.assert_called_once_with(args)


def test_shared_detector_defaults_honor_the_container_environment(monkeypatch):
    monkeypatch.setenv("BLOCKSCI_MIN_INPUT_COUNT", "15")
    monkeypatch.setenv("BLOCKSCI_JOINMARKET_MAX_DEPTH", "123")
    args = worker.parse_args(["analyze", "--run-dir", "/runs/run-a"])
    assert args.min_input_count == 15
    assert args.joinmarket_max_depth == 123
