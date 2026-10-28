from __future__ import annotations

import json
import tempfile
import queue
import threading
from pathlib import Path
from unittest import mock

from coinjoin_pipeline.execution.pbs.status import parse_qstat as _parse_qstat
from coinjoin_pipeline.watch import (
    PbsJob,
    _job_ids_from_frontend_log,
    _kubernetes_log_command,
    _newest_pod,
    _pbs_reader,
    build_parser,
    main,
)


def test_newest_pod_selects_latest_creation_timestamp() -> None:
    payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "old",
                        "creationTimestamp": "2026-07-22T10:00:00Z",
                    }
                },
                {
                    "metadata": {
                        "name": "new",
                        "creationTimestamp": "2026-07-22T11:00:00Z",
                    }
                },
            ]
        }
    )

    assert _newest_pod(payload) == "new"


def test_default_namespace_matches_pipeline_default() -> None:
    args = build_parser().parse_args(["--run-id", "run-1"])

    assert args.namespace == "coinjoin"


def test_kubernetes_log_command_supports_follow_and_timestamps() -> None:
    command = _kubernetes_log_command(
        ["kubectl", "--namespace", "man5-ns"],
        "outer-pod",
        "controller",
        tail=200,
        follow=True,
    )

    assert command == [
        "kubectl",
        "--namespace",
        "man5-ns",
        "logs",
        "pod/outer-pod",
        "-c",
        "controller",
        "--tail=200",
        "--timestamps=true",
        "--follow=true",
    ]


def test_main_builds_unified_all_component_sources() -> None:
    with tempfile.NamedTemporaryFile() as kubeconfig:
        with (
            mock.patch(
                "coinjoin_pipeline.watch._discover_engine",
                return_value="wasabi",
            ),
            mock.patch(
                "coinjoin_pipeline.watch._discover_pod",
                side_effect=["outer-pod", "coordinator-pod"],
            ) as discover,
            mock.patch("coinjoin_pipeline.watch.stream_sources", return_value=0) as stream,
        ):
            code = main(
                [
                    "--run-id",
                    "run-1",
                    "--namespace",
                    "man5-ns",
                    "--kubeconfig",
                    kubeconfig.name,
                    "--all",
                    "--no-follow",
                ]
            )

    assert code == 0
    sources = stream.call_args.args[0]
    assert set(sources) == {"controller", "uploader", "engine"}
    assert sources["controller"][-1] == "--timestamps=true"
    assert "--follow=true" not in sources["controller"]
    assert discover.call_args_list[0].args[1] == ("app.kubernetes.io/name=coinjoin-s3,coinjoin.run-id=run-1")
    assert discover.call_args_list[1].args[1] == ("app=wasabi-coordinator,coinjoin.run-id=run-1")


def test_main_all_discovers_joinmarket_engine_service() -> None:
    with tempfile.NamedTemporaryFile() as kubeconfig:
        with (
            mock.patch(
                "coinjoin_pipeline.watch._discover_engine",
                return_value="joinmarket",
            ),
            mock.patch(
                "coinjoin_pipeline.watch._discover_pod",
                side_effect=["outer-pod", "joinmarket-pod"],
            ) as discover,
            mock.patch("coinjoin_pipeline.watch.stream_sources", return_value=0) as stream,
        ):
            code = main(
                [
                    "--run-id",
                    "run-1",
                    "--namespace",
                    "man5-ns",
                    "--kubeconfig",
                    kubeconfig.name,
                    "--all",
                    "--no-follow",
                ]
            )

    assert code == 0
    assert set(stream.call_args.args[0]) == {"controller", "uploader", "engine"}
    assert discover.call_args_list[0].args[1] == ("app.kubernetes.io/name=coinjoin-s3,coinjoin.run-id=run-1")
    assert discover.call_args_list[1].args[1] == ("app=joinmarket-distributor,coinjoin.run-id=run-1")


def test_parse_qstat_extracts_state_and_wrapped_output_path() -> None:
    fields = _parse_qstat(
        """Job Id: 123.server
    Job_Name = blocksci_analysis_s3
    job_state = R
    Output_Path = frontend:/storage/brno2/home/user/
        blocksci_analysis_s3.o123
"""
    )

    assert fields["job_state"] == "R"
    assert fields["Output_Path"] == ("frontend:/storage/brno2/home/user/blocksci_analysis_s3.o123")


def test_frontend_log_discovers_all_s3_pbs_jobs() -> None:
    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as log:
        log.write("[pbs] Submitted coinjoin-analysis S3-compatible PBS job: 101.server\n")
        log.write("[pbs] Submitted blocksci S3-compatible PBS job: 102.server\n")
        log.flush()

        assert _job_ids_from_frontend_log(Path(log.name)) == {
            "coinjoin-analysis": "101.server",
            "blocksci": "102.server",
        }


def test_main_pbs_only_does_not_require_kubeconfig() -> None:
    job = PbsJob(
        stage="blocksci",
        job_id="102.server",
        output_path=Path("/tmp/blocksci.o102"),
        state="R",
    )
    with (
        mock.patch("coinjoin_pipeline.watch._pbs_job_details", return_value=job),
        mock.patch("coinjoin_pipeline.watch.stream_sources", return_value=0) as stream,
    ):
        code = main(
            [
                "--run-id",
                "run-1",
                "--pbs-only",
                "--pbs-job",
                "blocksci=102.server",
                "--no-follow",
            ]
        )

    assert code == 0
    assert stream.call_args.args[0] == {}
    assert stream.call_args.kwargs["pbs_jobs"] == {"pbs:blocksci": job}


def test_main_uses_host_runs_root_for_pbs_job_discovery(tmp_path: Path) -> None:
    marker_dir = tmp_path / "run-1" / ".pbs"
    marker_dir.mkdir(parents=True)
    (marker_dir / "blocksci.jobid").write_text("102.server\n", encoding="utf-8")
    job = PbsJob(
        stage="blocksci",
        job_id="102.server",
        output_path=tmp_path / "blocksci.o102",
        state="R",
    )

    with (
        mock.patch("coinjoin_pipeline.watch._pbs_job_details", return_value=job) as details,
        mock.patch("coinjoin_pipeline.watch.stream_sources", return_value=0) as stream,
    ):
        code = main(
            ["--run-id", "run-1", "--pbs-only", "--no-follow"],
            runs_root=tmp_path,
        )

    assert code == 0
    details.assert_called_once_with("blocksci", "102.server")
    assert stream.call_args.kwargs["pbs_jobs"] == {"pbs:blocksci": job}


def test_main_skips_stale_pbs_jobs_and_still_watches_live_ones(tmp_path: Path, capsys) -> None:
    """One unresolvable .jobid must not hide every other stage.

    .jobid files are never pruned, so an old job that qstat can no longer
    describe would otherwise make the whole watcher exit before showing
    anything.
    """
    marker_dir = tmp_path / "run-1" / ".pbs"
    marker_dir.mkdir(parents=True)
    (marker_dir / "blocksci.jobid").write_text("102.server\n", encoding="utf-8")
    (marker_dir / "coinjoin-analysis.jobid").write_text("7.server\n", encoding="utf-8")
    job = PbsJob(
        stage="blocksci",
        job_id="102.server",
        output_path=tmp_path / "blocksci.o102",
        state="R",
    )

    def details(stage: str, job_id: str) -> PbsJob:
        if job_id == "7.server":
            raise RuntimeError("cannot inspect PBS job 7.server: Unknown Job Id")
        return job

    with (
        mock.patch("coinjoin_pipeline.watch._pbs_job_details", side_effect=details),
        mock.patch("coinjoin_pipeline.watch.stream_sources", return_value=0) as stream,
    ):
        code = main(
            ["--run-id", "run-1", "--pbs-only", "--no-follow"],
            runs_root=tmp_path,
        )

    assert code == 0
    assert stream.call_args.kwargs["pbs_jobs"] == {"pbs:blocksci": job}
    assert "skipping coinjoin-analysis=7.server" in capsys.readouterr().err


def test_main_fails_when_no_pbs_job_can_be_inspected(tmp_path: Path, capsys) -> None:
    marker_dir = tmp_path / "run-1" / ".pbs"
    marker_dir.mkdir(parents=True)
    (marker_dir / "blocksci.jobid").write_text("102.server\n", encoding="utf-8")

    with (
        mock.patch(
            "coinjoin_pipeline.watch._pbs_job_details",
            side_effect=RuntimeError("Unknown Job Id"),
        ),
        mock.patch("coinjoin_pipeline.watch.stream_sources", return_value=0) as stream,
    ):
        code = main(
            ["--run-id", "run-1", "--pbs-only", "--no-follow"],
            runs_root=tmp_path,
        )

    assert code == 2
    stream.assert_not_called()
    assert "none of the discovered PBS jobs could be inspected" in capsys.readouterr().err


def test_frontend_log_is_replayed_whole_while_pbs_keeps_the_tail(tmp_path: Path) -> None:
    log = tmp_path / "frontend.log"
    log.write_text("[dry-run] action: full-run\n")
    job = PbsJob(stage="blocksci", job_id="102.server", output_path=Path("/tmp/blocksci.o102"), state="R")
    with (
        mock.patch("coinjoin_pipeline.watch._pbs_job_details", return_value=job),
        mock.patch("coinjoin_pipeline.watch.stream_sources", return_value=0) as stream,
    ):
        code = main(["--run-id", "run-1", "--pbs-only", "--frontend-log", str(log), "--pbs-job", "blocksci=102.server"])

    assert code == 0
    assert stream.call_args.args[0] == {"frontend": ["tail", "-n", "+1", "-f", str(log)]}
    assert stream.call_args.kwargs["tail"] == 200


def test_main_discovers_mainnet_component_logs_in_the_selected_run(tmp_path: Path) -> None:
    run = tmp_path / "run-1"
    markers = run / ".pbs"
    markers.mkdir(parents=True)
    (markers / "mainnet-producer.jobid").write_text("101.server")
    job = PbsJob("mainnet-producer", "101.server", tmp_path / "pbs.out", "R")
    with (
        mock.patch("coinjoin_pipeline.watch._pbs_job_details", return_value=job),
        mock.patch("coinjoin_pipeline.watch.stream_sources", return_value=0) as stream,
    ):
        assert main(["--run-id", "run-1", "--run-dir", str(run), "--pbs-only", "--no-follow"]) == 0
    discovered = stream.call_args.kwargs["pbs_jobs"]["pbs:mainnet-producer"]
    assert discovered.log_dir == run / "logs/mainnet-producer"


def test_pbs_reader_streams_late_component_logs_without_pbs_stdout(tmp_path: Path) -> None:
    logs = tmp_path / "logs/mainnet-producer"
    job = PbsJob("mainnet-producer", "101.server", tmp_path / "not-delivered.out", "R", logs)
    messages = queue.Queue()
    stop = mock.Mock()
    stop.is_set.return_value = False
    polls = 0

    def advance(_seconds):
        nonlocal polls
        polls += 1
        if polls == 1:
            logs.mkdir(parents=True)
            (logs / "core-producer.log").write_text("old\rcore 1\rcore 2\r")
            # A neighbouring stage/run must never be picked up.
            (logs.parent / "other-stage").mkdir()
            (logs.parent / "other-stage/dumplings.log").write_text("foreign\n")
        elif polls == 2:
            with (logs / "core-producer.log").open("a") as file:
                file.write("core 3\n")
            (logs / "dumplings.log").write_text("scanner started\n")
        else:
            stop.is_set.return_value = True

    stop.wait.side_effect = advance
    with mock.patch("coinjoin_pipeline.watch._pbs_job_details", return_value=job):
        _pbs_reader("pbs:producer", job, messages, stop, tail=2, follow=True)
    lines = [line for _source, line in messages.queue]
    assert lines[2:] == [
        "[core-producer] core 1",
        "[core-producer] core 2",
        "[core-producer] core 3",
        "[dumplings] scanner started",
        None,
    ]


def test_pbs_reader_no_follow_reads_components_and_pbs_with_independent_tails(tmp_path: Path) -> None:
    logs = tmp_path / "mainnet-blocksci"
    logs.mkdir()
    (logs / "blocksci.log").write_text("old parse\nparse 50%\n")
    output = tmp_path / "pbs.out"
    output.write_text("old PBS\njob done\n")
    job = PbsJob("mainnet-blocksci", "102.server", output, "F", logs)
    messages = queue.Queue()
    with mock.patch("coinjoin_pipeline.watch._pbs_job_details") as query:
        _pbs_reader("pbs:blocksci", job, messages, threading.Event(), tail=1, follow=False)
    assert [line for _, line in messages.queue][1:] == ["job done", "[blocksci] parse 50%", None]
    query.assert_not_called()


def test_pbs_reader_drains_final_component_lines_after_job_finishes(tmp_path: Path) -> None:
    logs = tmp_path / "mainnet-blocksci"
    logs.mkdir()
    output = logs / "blocksci.log"
    output.write_text("parsing\n")
    job = PbsJob("mainnet-blocksci", "102.server", tmp_path / "pbs.out", "R", logs)
    finished = PbsJob("mainnet-blocksci", "102.server", job.output_path, "F", logs)
    stop = mock.Mock()
    stop.is_set.return_value = False
    polls = 0

    def advance(_seconds):
        nonlocal polls
        polls += 1
        if polls == 2:
            with output.open("a") as file:
                file.write("finished\n")
        assert polls <= 4, "watcher should finish after draining terminal logs"

    stop.wait.side_effect = advance
    messages = queue.Queue()
    with mock.patch("coinjoin_pipeline.watch._pbs_job_details", return_value=finished):
        _pbs_reader("pbs:blocksci", job, messages, stop, tail=200, follow=True)
    lines = [line for _, line in messages.queue]
    assert "[blocksci] parsing" in lines
    assert "[blocksci] finished" in lines
    assert lines[-1] is None
