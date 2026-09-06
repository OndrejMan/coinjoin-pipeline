import subprocess
import sys
from pathlib import Path
from unittest import mock


PIPELINE_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PIPELINE_ROOT))

from client.kubernetes import collect_s3_emulation_diagnostics


def test_quota_failure_is_summarized_even_before_controller_log_tail() -> None:
    quota_failure = (
        "ERROR | Terminating exception: Kubernetes CPU quota exhausted while creating pod "
        "'wasabi-client-000' in namespace 'man5-ns': quota 'default-cldp6' rejected "
        "limits.cpu=1 (used limits.cpu=31500m; limit limits.cpu=32)."
    )
    controller_output = "\n".join(
        [quota_failure, *(f"diagnostic line {index}" for index in range(150))]
    )
    completed = [
        subprocess.CompletedProcess([], 0, stdout="job description", stderr=""),
        subprocess.CompletedProcess([], 0, stdout=controller_output, stderr=""),
        subprocess.CompletedProcess([], 0, stdout="uploader logs", stderr=""),
    ]

    with mock.patch("client.kubernetes.subprocess.run", side_effect=completed) as run:
        diagnostics = collect_s3_emulation_diagnostics(
            Path("/kube/config"), "man5-ns", "job-1"
        )

    assert "--- controller failure summary ---\n" + quota_failure in diagnostics
    controller_tail = diagnostics.split("--- controller logs ---\n", 1)[1].split(
        "--- uploader logs ---", 1
    )[0]
    assert quota_failure not in controller_tail
    assert "diagnostic line 50" in controller_tail
    assert "diagnostic line 49" not in controller_tail
    assert run.call_args_list[1].args[0][-1] == "--tail=-1"
