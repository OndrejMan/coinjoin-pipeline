from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import mock

from coinjoin_pipeline.watch import (
    PbsJob,
    _job_ids_from_frontend_log,
    _kubernetes_log_command,
    _newest_pod,
    _parse_qstat,
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
                "coinjoin_pipeline.watch._discover_pod",
                side_effect=["outer-pod", "coordinator-pod"],
            ),
            mock.patch(
                "coinjoin_pipeline.watch.stream_sources", return_value=0
            ) as stream,
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
    assert set(sources) == {"controller", "uploader", "coordinator"}
    assert sources["controller"][-1] == "--timestamps=true"
    assert "--follow=true" not in sources["controller"]


def test_parse_qstat_extracts_state_and_wrapped_output_path() -> None:
    fields = _parse_qstat(
        """Job Id: 123.server
    Job_Name = blocksci_analysis_s3
    job_state = R
    Output_Path = frontend:/storage/brno2/home/xman/
        blocksci_analysis_s3.o123
"""
    )

    assert fields["job_state"] == "R"
    assert fields["Output_Path"] == (
        "frontend:/storage/brno2/home/xman/blocksci_analysis_s3.o123"
    )


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
