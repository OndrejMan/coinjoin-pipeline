"""The rendered Python stage jobs, run end to end against a directory-backed s5cmd."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coinjoin_pipeline.execution.pbs.scripts import embedded_file
from coinjoin_pipeline.execution.pbs.templates_s3 import (
    render_blocksci_s3_pbs,
    render_coinjoin_analysis_dumplings_s3_pbs,
    render_coinjoin_analysis_s3_pbs,
    render_mappings_s3_pbs,
    render_unified_report_s3_pbs,
)
from coinjoin_pipeline.storage.s3 import S3Target

FAKE_S5CMD_DIR = Path(__file__).resolve().parents[1] / "support/persistent-index"
RUN = "s3://bucket/runs/run-1"
REPORT_INPUTS = {
    ".pipeline/exporters/worker.py": "",
    "coinjoin_emulator_data/scenario.json": "{}",
    "coinjoin-analysis_data/coinjoin_tx_info.json": '{"coinjoins": {}}',
    "blocksci-analysis_data/blocksci_analysis.json": "{}",
}
ANALYSIS_INPUTS = {
    "coinjoin_emulator_data/data/wallet/coinjoins.json": "{}",
    # Never read by the analyzer; the job must not download it.
    "bitcoin_data/regtest/blocks/blk00000.dat": "blocks",
}
# Records its call and writes what the stage's container would: the report into the
# exporters' run root, or the baseline into the bound analysis output.
FAKE_SINGULARITY = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SINGULARITY_CALLS"
[ -n "${SINGULARITY_SLEEP:-}" ] && exec sleep "$SINGULARITY_SLEEP"
for argument in "$@"; do
  case "$argument" in
    *:/runs/emulation/logs:rw) root="${argument%%:*}" ;;
    *:/runs/emulation/selected/*/data:ro) data="${argument%%:*}" ;;
    *:/runs/emulation/selected/*:rw) output="${argument%%:*}" ;;
    PBS_RUN_ID=*) run_id="${argument#PBS_RUN_ID=}" ;;
  esac
done
if [ -n "${root:-}" ]; then
  mkdir -p "$root/$run_id/coinjoinPipeline_data"
  printf '{}\\n' > "$root/$run_id/coinjoinPipeline_data/unified_report.json"
fi
for argument in "$@"; do
  case "$argument" in
    *:/work:rw) work="${argument%%:*}" ;;
  esac
done
case "$1" in
  pull) printf 'sif of %s\\n' "$4" > "$3"; exit 0 ;;
esac
for argument in "$@"; do
  case "$argument" in
    --output) take_output=1 ;;
    *) if [ -n "${take_output:-}" ]; then tool_output="$argument"; take_output=; fi ;;
  esac
done
if [ -n "${tool_output:-}" ]; then
  printf '{"summary": {"errors": %s}}\\n' "${TOOL_ERRORS:-0}" > "$tool_output"
  exit "${TOOL_EXIT:-0}"
fi
# exec --bind <work> <sif> python3 -c ...: the enumerator image's Python, here the host's.
if [ "${5:-}" = python3 ] && [ "${6:-}" = -c ]; then
  shift 4
  exec "$@"
fi
if [ "${*: -1}" = "/opt/coinjoin-analysis/data/wasabi2/false_cjtxs.json" ]; then
  [ -n "${NO_FALSE_CJTXS:-}" ] && exit 1
  printf '{"curated": true}\\n'
  exit 0
fi
if [ -n "${work:-}" ]; then
  ls "$work/dumplings/Scanner" > "$SINGULARITY_CALLS.scanner"
  mkdir -p "$work/dumplings/Scanner/wasabi2"
  printf '{"coinjoins": {"a": {}, "b": {}}}\\n' > "$work/dumplings/Scanner/wasabi2/coinjoin_tx_info.json"
fi
if [ -n "${output:-}" ]; then
  ls "$(dirname "$output")" > "$SINGULARITY_CALLS.tree"
  if [ -f "$data/wallet/coinjoins.json" ]; then
    printf '{"coinjoins": {}}\\n' > "$output/coinjoin_tx_info.json"
  fi
fi
"""


@pytest.fixture
def job(tmp_path: Path) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    singularity = bin_dir / "singularity"
    singularity.write_text(FAKE_SINGULARITY)
    singularity.chmod(0o700)
    credentials = tmp_path / "credentials"
    credentials.write_text("[coinjoin]\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return {"bin": bin_dir, "credentials": credentials, "scratch": scratch, "s3": tmp_path / "s3"}


def remote(job: dict[str, Path], relative: str) -> Path:
    return job["s3"] / RUN.removeprefix("s3://") / relative


def report_script(job: dict[str, Path], **options: bool) -> str:
    target = S3Target(
        artifact_uri="s3://bucket/runs",
        run_id="run-1",
        endpoint_url="https://s3.example.org",
        credentials_file=str(job["credentials"]),
        profile="coinjoin",
    )
    return render_unified_report_s3_pbs(target, image="docker://report:1", command="report-command", **options)


def start(job: dict[str, Path], script: str, **environment: str) -> subprocess.Popen:
    path = job["scratch"].parent / "job.pbs"
    path.write_text(script)
    return subprocess.Popen(
        ["bash", str(path)],
        env={
            **os.environ,
            "PATH": os.pathsep.join(
                (str(job["bin"]), str(FAKE_S5CMD_DIR), str(Path(sys.executable).parent), os.environ["PATH"])
            ),
            "SCRATCHDIR": str(job["scratch"]),
            "FAKE_S3": str(job["s3"]),
            "SINGULARITY_CALLS": str(job["scratch"].parent / "singularity.calls"),
            **environment,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def upload_inputs(job: dict[str, Path], inputs: dict[str, str]) -> None:
    for relative, content in inputs.items():
        remote(job, relative).parent.mkdir(parents=True, exist_ok=True)
        remote(job, relative).write_text(content)


def test_report_job_publishes_the_report_and_its_done_marker(job: dict[str, Path]) -> None:
    upload_inputs(job, REPORT_INPUTS)
    process = start(job, report_script(job))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 0, output
    assert remote(job, "coinjoinPipeline_data/unified_report.json").is_file()
    assert remote(job, ".pbs/unified-report.done").read_text() == "done\n"
    assert not remote(job, ".pbs/unified-report.failed").exists()
    call = (job["scratch"].parent / "singularity.calls").read_text()
    assert f"--bind {job['scratch']}/coinjoin-run:/runs/emulation/logs:rw" in call
    assert "--env PBS_RUN_ID=run-1 docker://report:1 bash -c" in call
    assert call.rstrip().endswith('cd "/runs/emulation/logs/$PBS_RUN_ID" && report-command')
    # The scratch is emptied once the markers are in S3; the directory itself stays.
    assert list(job["scratch"].iterdir()) == []


def test_report_job_without_an_analyzer_output_publishes_a_failed_marker(job: dict[str, Path]) -> None:
    upload_inputs(job, {name: content for name, content in REPORT_INPUTS.items() if "blocksci" not in name})
    process = start(job, report_script(job))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 1
    assert "requires blocksci-analysis_data/blocksci_analysis.json" in output
    # The same output reaches S3, for a host PBS cannot copy the output file back to.
    uploaded = remote(job, "logs/unified-report.pbs.log").read_text()
    assert "requires blocksci-analysis_data/blocksci_analysis.json" in uploaded
    assert remote(job, ".pbs/unified-report.failed").read_text() == "failed\n"
    assert not remote(job, ".pbs/unified-report.done").exists()
    assert not (job["scratch"].parent / "singularity.calls").exists()


def test_terminated_report_job_still_publishes_a_failed_marker(job: dict[str, Path]) -> None:
    upload_inputs(job, REPORT_INPUTS)
    process = start(job, report_script(job), SINGULARITY_SLEEP="30")
    calls = job["scratch"].parent / "singularity.calls"
    deadline = time.monotonic() + 30
    while not calls.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=60)

    assert process.returncode == 143
    assert remote(job, ".pbs/unified-report.failed").is_file()
    assert not remote(job, ".pbs/unified-report.done").exists()


def test_report_job_downloads_mappings_only_when_requested(job: dict[str, Path]) -> None:
    def downloads(script: str) -> list[str]:
        return json.loads(embedded_file(script, "settings.json"))["downloads"]

    assert "coinjoin-mappings_data" not in downloads(report_script(job))
    assert "coinjoin-mappings_data" in downloads(report_script(job, include_mappings=True))


def analysis_script(job: dict[str, Path]) -> str:
    target = S3Target(
        artifact_uri="s3://bucket/runs",
        run_id="run-1",
        endpoint_url="https://s3.example.org",
        credentials_file=str(job["credentials"]),
        profile="coinjoin",
    )
    return render_coinjoin_analysis_s3_pbs(target, image="docker://coinjoin:1", command="collect")


def test_analysis_job_downloads_only_emulator_data_and_publishes_the_baseline(job: dict[str, Path]) -> None:
    upload_inputs(job, ANALYSIS_INPUTS)
    process = start(job, analysis_script(job))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 0, output
    assert remote(job, "coinjoin-analysis_data/coinjoin_tx_info.json").is_file()
    assert remote(job, ".pbs/coinjoin-analysis.done").read_text() == "done\n"
    calls = job["scratch"].parent / "singularity.calls"
    work = f"{job['scratch']}/coinjoin-run/run-1"
    call = calls.read_text()
    assert f"--bind {job['scratch']}/coinjoin-analysis-selected:/runs/emulation/selected:rw" in call
    assert f"--bind {work}/coinjoin-analysis_data:/runs/emulation/selected/run-1:rw" in call
    assert f"--bind {work}/coinjoin_emulator_data/data:/runs/emulation/selected/run-1/data:ro" in call
    assert call.rstrip().endswith('cd "/runs/emulation/selected/$PBS_RUN_ID" && collect')
    assert "bitcoin_data" not in Path(f"{calls}.tree").read_text().split()
    assert list(job["scratch"].iterdir()) == []


def test_analysis_job_without_emulator_data_publishes_a_failed_marker(job: dict[str, Path]) -> None:
    upload_inputs(job, {"bitcoin_data/regtest/blocks/blk00000.dat": "blocks"})
    process = start(job, analysis_script(job))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 1
    assert "requires coinjoin_emulator_data/data" in output
    assert remote(job, ".pbs/coinjoin-analysis.failed").is_file()
    assert not remote(job, ".pbs/coinjoin-analysis.done").exists()


def test_analysis_job_that_writes_no_baseline_fails(job: dict[str, Path]) -> None:
    upload_inputs(job, {"coinjoin_emulator_data/data/other.txt": "x"})
    process = start(job, analysis_script(job))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 1
    assert "did not produce coinjoin-analysis_data/coinjoin_tx_info.json" in output
    assert remote(job, ".pbs/coinjoin-analysis.failed").is_file()
    assert not remote(job, "coinjoin-analysis_data/coinjoin_tx_info.json").exists()


DUMPLINGS = "s3://bucket/dumplings"


def dumplings_script(job: dict[str, Path]) -> str:
    target = S3Target(
        artifact_uri="s3://bucket/runs",
        run_id="run-1",
        endpoint_url="https://s3.example.org",
        credentials_file=str(job["credentials"]),
        profile="coinjoin",
    )
    return render_coinjoin_analysis_dumplings_s3_pbs(
        target, "docker://coinjoin:1", dumplings_uri=DUMPLINGS, coinjoin_type="wasabi2"
    )


def publish_dumplings(job: dict[str, Path], *, manifest_sizes: dict[str, int] | None = None) -> None:
    files = {"Wasabi2CoinJoins.txt": "cj\n", "Wasabi2CoinJoins.txt.json": "stale cache"}
    root = job["s3"] / DUMPLINGS.removeprefix("s3://")
    for name, content in files.items():
        (root / "Scanner").mkdir(parents=True, exist_ok=True)
        (root / "Scanner" / name).write_text(content)
    sizes = {name: len(content) for name, content in files.items()}
    (root / "manifest.json").write_text(
        json.dumps({"last_processed_height": 900, "files": sizes if manifest_sizes is None else manifest_sizes})
    )


def test_dumplings_baseline_publishes_the_baseline_curated_false_positives_and_snapshot(
    job: dict[str, Path],
) -> None:
    publish_dumplings(job)
    process = start(job, dumplings_script(job))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 0, output
    assert "baseline of 2 coinjoins through height 900" in output
    published = remote(job, "coinjoin-analysis_data")
    assert json.loads((published / "coinjoin_tx_info.json").read_text())["coinjoins"].keys() == {"a", "b"}
    assert json.loads((published / "false_cjtxs.json").read_text()) == {"curated": True}
    assert json.loads((published / "dumplings-snapshot.json").read_text())["last_processed_height"] == 900
    # A stale coinjoin-analysis cache next to the Dumplings files would replace them.
    assert (job["scratch"].parent / "singularity.calls.scanner").read_text().split() == ["Wasabi2CoinJoins.txt"]
    assert "--cjtype ww2 --action process_dumplings" in (job["scratch"].parent / "singularity.calls").read_text()
    assert remote(job, ".pbs/coinjoin-analysis.done").is_file()


def test_dumplings_baseline_without_curated_false_positives_still_succeeds(job: dict[str, Path]) -> None:
    publish_dumplings(job)
    process = start(job, dumplings_script(job), NO_FALSE_CJTXS="1")
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 0, output
    assert "the image has no curated false_cjtxs.json" in output
    assert not remote(job, "coinjoin-analysis_data/false_cjtxs.json").exists()
    assert remote(job, "coinjoin-analysis_data/coinjoin_tx_info.json").is_file()


def test_dumplings_baseline_refuses_a_copy_that_differs_from_its_manifest(job: dict[str, Path]) -> None:
    publish_dumplings(job, manifest_sizes={"Wasabi2CoinJoins.txt": 999})
    process = start(job, dumplings_script(job))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 1
    assert "does not match its manifest" in output
    assert remote(job, ".pbs/coinjoin-analysis.failed").is_file()
    assert not (job["scratch"].parent / "singularity.calls").exists()


def mappings_script(job: dict[str, Path]) -> str:
    target = S3Target(
        artifact_uri="s3://bucket/runs",
        run_id="run-1",
        endpoint_url="https://s3.example.org",
        credentials_file=str(job["credentials"]),
        profile="coinjoin",
    )
    return render_mappings_s3_pbs(target, "docker://enumerator:1", "docker://sake:1")


def test_mappings_job_publishes_the_combined_mappings(job: dict[str, Path]) -> None:
    upload_inputs(job, {"coinjoin-analysis_data/coinjoin_tx_info.json": '{"coinjoins": {}}'})
    process = start(job, mappings_script(job))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 0, output
    combined = json.loads(remote(job, "coinjoin-mappings_data/coinjoin_mappings.json").read_text())
    assert combined["status"] == "complete"
    assert combined["provenance"]["enumerator_image"] == "docker://enumerator:1"
    assert combined["provenance"]["sake_image_digest"].startswith("sha256:")
    assert remote(job, ".pbs/coinjoin-mappings.done").is_file()


def test_failed_mappings_still_publish_their_partial_output(job: dict[str, Path]) -> None:
    upload_inputs(job, {"coinjoin-analysis_data/coinjoin_tx_info.json": '{"coinjoins": {}}'})
    process = start(job, mappings_script(job), TOOL_EXIT="2")
    output, _ = process.communicate(timeout=60)

    assert process.returncode != 0, output
    assert remote(job, "coinjoin-mappings_data/enumerator.json").is_file()
    assert remote(job, ".pbs/coinjoin-mappings.failed").is_file()
    assert not remote(job, ".pbs/coinjoin-mappings.done").exists()


def test_mappings_job_without_a_baseline_fails(job: dict[str, Path]) -> None:
    process = start(job, mappings_script(job))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 1
    assert "CoinJoin mappings require coinjoin-analysis_data/coinjoin_tx_info.json" in output
    assert remote(job, ".pbs/coinjoin-mappings.failed").is_file()


def test_blocksci_job_without_a_bitcoin_datadir_fails_before_parsing(job: dict[str, Path]) -> None:
    upload_inputs(job, {"coinjoin_emulator_data/data/btc-node/block_3.json": "{}", ".pipeline/exporters/x.py": ""})
    target = S3Target(
        artifact_uri="s3://bucket/runs",
        run_id="run-1",
        endpoint_url="https://s3.example.org",
        credentials_file=str(job["credentials"]),
        profile="coinjoin",
    )
    process = start(job, render_blocksci_s3_pbs(target, "docker://blocksci:1", "parse", include_report=False))
    output, _ = process.communicate(timeout=60)

    assert process.returncode == 1
    assert "BlockSci requires a Bitcoin datadir containing regtest/blocks" in output
    assert remote(job, ".pbs/blocksci.failed").is_file()
    assert not remote(job, "blocksci-parse_data/manifest.json").exists()
    assert not (job["scratch"].parent / "singularity.calls").exists()
