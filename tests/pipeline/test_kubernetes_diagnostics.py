import subprocess
import sys
from pathlib import Path
from unittest import mock

PIPELINE_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PIPELINE_ROOT))

from coinjoin_pipeline.execution.kubernetes import collect_s3_emulation_diagnostics


def test_quota_failure_summary_keeps_the_complete_controller_log() -> None:
    quota_failure = (
        "ERROR | Terminating exception: Kubernetes CPU quota exhausted while creating pod "
        "'wasabi-client-000' in namespace 'man5-ns': quota 'default-cldp6' rejected "
        "limits.cpu=1 (used limits.cpu=31500m; limit limits.cpu=32)."
    )
    controller_output = "\n".join([quota_failure, *(f"diagnostic line {index}" for index in range(150))])
    completed = [
        subprocess.CompletedProcess([], 0, stdout="job description", stderr=""),
        subprocess.CompletedProcess([], 0, stdout=controller_output, stderr=""),
        subprocess.CompletedProcess([], 0, stdout="uploader logs", stderr=""),
    ]

    with mock.patch("coinjoin_pipeline.execution.kubernetes.subprocess.run", side_effect=completed) as run:
        diagnostics = collect_s3_emulation_diagnostics(Path("/kube/config"), "man5-ns", "job-1")

    assert "--- controller failure summary ---\n" + quota_failure in diagnostics
    controller_log = diagnostics.split("--- controller logs ---\n", 1)[1].split("--- uploader logs ---", 1)[0]
    assert controller_log.rstrip("\n") == controller_output
    assert run.call_args_list[1].args[0][-1] == "--tail=-1"


def test_client_gone_failure_is_summarized_before_cleanup_tail() -> None:
    client_gone = (
        "Terminating exception: client wasabi-client-004 is unreachable at "
        "wasabi-client-004:37128 (container state: Completed). Underlying error: "
        "Connection refused"
    )
    controller_output = "\n".join(
        [
            client_gone,
            *(f"Deleted pod wasabi-client-{index:03d}" for index in range(150)),
        ]
    )
    completed = [
        subprocess.CompletedProcess([], 0, stdout="job description", stderr=""),
        subprocess.CompletedProcess([], 0, stdout=controller_output, stderr=""),
        subprocess.CompletedProcess([], 0, stdout="uploader logs", stderr=""),
    ]

    with mock.patch("coinjoin_pipeline.execution.kubernetes.subprocess.run", side_effect=completed):
        diagnostics = collect_s3_emulation_diagnostics(Path("/kube/config"), "man5-ns", "job-1")

    assert "--- controller failure summary ---\n" + client_gone in diagnostics


def test_original_traceback_survives_a_long_cleanup_log() -> None:
    traceback = (
        'Traceback (most recent call last):\n'
        '  File "/app/manager/engine/engine_base.py", line 360, in fund_distributor\n'
        '    self.distributor.get_new_address()\n'
        'requests.exceptions.ConnectionError: distributor DNS lookup failed'
    )
    controller_output = "\n".join(
        [traceback, *(f"cleanup line {index}" for index in range(150))]
    )
    completed = [
        subprocess.CompletedProcess([], 0, stdout="job description", stderr=""),
        subprocess.CompletedProcess([], 0, stdout=controller_output, stderr=""),
        subprocess.CompletedProcess([], 0, stdout="uploader logs", stderr=""),
    ]

    with mock.patch("coinjoin_pipeline.execution.kubernetes.subprocess.run", side_effect=completed):
        diagnostics = collect_s3_emulation_diagnostics(Path("/kube/config"), "man5-ns", "job-1")

    assert traceback in diagnostics
    assert "cleanup line 149" in diagnostics
