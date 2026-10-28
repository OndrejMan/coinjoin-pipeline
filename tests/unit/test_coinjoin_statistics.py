"""The CoinJoin Statistics workflow script, run against a fake coinjoin-analysis interpreter."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution import coinjoin_tools
from coinjoin_pipeline.execution.pbs.templates_s3 import render_coinjoin_tools_s3_pbs
from coinjoin_pipeline.storage.s3 import S3Target

SCRIPT = coinjoin_tools.WORKFLOW_SCRIPTS["coinjoin_statistics"]
# Records each call and leaves the files the real parse_dumplings step would create.
FAKE_PYTHON = r"""#!/usr/bin/env python3
import os, pathlib, sys
if sys.argv[1] == "-":
    os.execvp("python3", ["python3", *sys.argv[1:]])
args = sys.argv[1:]
with open(os.environ["TEST_CALLS"], "a", encoding="utf-8") as calls:
    calls.write(" ".join(args) + "\n")
if args[1] == "cj_process.file_check":
    sys.exit(0)
scanner = pathlib.Path(args[args.index("--target-path") + 1]) / "Scanner"
action = args[args.index("--action") + 1] if "--action" in args else ""
switches = args[args.index("--env_vars") + 1] if "--env_vars" in args else ""
cjtype = args[args.index("--cjtype") + 1]
pools = {"ww2": ["wasabi2", "wasabi2_others", "wasabi2_zksnacks"], "ww1": ["wasabi1"], "jm": ["joinmarket_all"]}[cjtype]
if action == "process_dumplings":
    for pool in pools:
        interval = scanner / pool / "2024-05-01 00-00-00--2024-06-01 00-00-00_unknown-static-100-1utxo"
        interval.mkdir(parents=True)
        (scanner / pool / "coinjoin_tx_info.json").write_text("{}")
        (interval / "coinjoin_tx_info.json").write_text("{}")
if action == "split_coordinators" and cjtype == "ww2":
    (scanner / "wasabi2_kruw").mkdir()
if "ANALYSIS_LIQUIDITY=True" in switches:
    (scanner / f"{pools[0]}.html").write_text("<p>liquidity</p>")
    (scanner / f"liquidity_summary_{pools[0]}.json").write_text("{}")
if action == "plot_coinjoins":
    (scanner / pools[0] / f"{pools[0]}_cummul_values_norm.png").write_text("png")
if os.environ.get("TEST_FAIL") and os.environ["TEST_FAIL"] in " ".join(args):
    sys.exit(3)
"""


@pytest.fixture
def workflow(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").write_text(FAKE_PYTHON)
    (bin_dir / "python").chmod(0o755)
    data = tmp_path / "data"
    for protocol, files in {
        "wasabi2": ("fee_rates.json", "false_cjtxs.json", "txid_coord.json"),
        "wasabi1": ("fee_rates.json", "false_cjtxs.json"),
        "joinmarket": ("fee_rates.json", "false_cjtxs.json"),
    }.items():
        (data / protocol).mkdir(parents=True)
        for name in files:
            (data / protocol / name).write_text("{}")
    work = tmp_path / "work"
    snapshot = work / "input"
    snapshot.mkdir(parents=True)
    for name in ("Wasabi2CoinJoins.txt", "WasabiCoinJoins.txt", "OtherCoinJoins.txt", "Wasabi2CoinJoins.txt.json"):
        (snapshot / name).write_text("")
    (snapshot / "LastProcessedBlockHeight.txt").write_text("870000\n")
    calls = tmp_path / "calls.txt"

    def run(*protocols, **env):
        result = subprocess.run(
            ["bash", str(SCRIPT), *protocols],
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "STATISTICS_ROOT": str(work),
                "COINJOIN_ANALYSIS_DATA": str(data),
                "TEST_CALLS": str(calls),
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        lines = calls.read_text().splitlines() if calls.exists() else []
        return result, [line.removeprefix("-m cj_process.") for line in lines]

    return work, run


def test_runs_the_daily_sequence_in_order_and_publishes_only_the_site(workflow):
    work, run = workflow
    result, calls = run("joinmarket", "wasabi2", "wasabi1")
    assert result.returncode == 0, result.stderr
    steps = [call.split(" --target-path")[0] for call in calls]
    assert steps[0] == "parse_dumplings --cjtype ww2 --action process_dumplings"
    assert steps.index("parse_dumplings --cjtype ww2 --action split_coordinators") < steps.index(
        "parse_dumplings --cjtype ww2 --env_vars ANALYSIS_LIQUIDITY=True"
    )
    # JoinMarket comes last and restores the Wasabi false positives first.
    first_jm = next(index for index, step in enumerate(steps) if "--cjtype jm" in step)
    assert all("--cjtype jm" not in step for step in steps[:first_jm])
    assert steps[first_jm] == "parse_dumplings --cjtype jm --env_vars RESTORE_FALSE_POSITIVES_FOR_OTHERS=True"
    assert steps[-1] == f"file_check {work}/.statistics/Scanner/"
    site = work / "site"
    assert (site / "wasabi2/wasabi2_cummul_values_norm.png").is_file()
    assert (site / "texts/wasabi2.html").read_text() == "<p>liquidity</p>"
    assert (site / "liquidity_summary_joinmarket_all.json").is_file()
    assert (site / "wasabi2_kruw/fee_rates.json").is_file()
    assert not list(site.rglob("coinjoin_tx_info.json"))
    summary = json.loads((site / "summary.json").read_text())
    assert summary["lastProcessedBlockHeight"] == "870000"
    assert summary["protocols"] == ["joinmarket", "wasabi2", "wasabi1"]
    # The snapshot copy and the processed data are not uploaded with the result.
    assert not (work / ".statistics").exists()
    assert not any((work / "input").iterdir())


def test_joinmarket_alone_skips_the_wasabi_restore(workflow):
    _, run = workflow
    result, calls = run("joinmarket")
    assert result.returncode == 0, result.stderr
    assert not any("RESTORE_FALSE_POSITIVES_FOR_OTHERS" in call for call in calls)
    assert "Wasabi false positives are not restored" in result.stdout


def test_stale_converted_dumplings_files_are_not_reused(workflow):
    work, run = workflow
    marker = work / "input/Wasabi2CoinJoins.txt.json"
    result, _ = run("wasabi2", TEST_FAIL="process_dumplings")
    assert result.returncode == 3
    assert not marker.exists()


@pytest.mark.parametrize(
    "protocols,message",
    [((), "Choose at least one protocol"), (("whirlpool",), "Unsupported protocol")],
)
def test_rejects_unknown_protocol_selections(workflow, protocols, message):
    _, run = workflow
    result, calls = run(*protocols)
    assert result.returncode == 2
    assert message in result.stderr
    assert calls == []


def test_missing_dumplings_file_stops_before_processing(workflow):
    work, run = workflow
    (work / "input/OtherCoinJoins.txt").unlink()
    result, calls = run("joinmarket")
    assert result.returncode == 1
    assert "no OtherCoinJoins.txt for joinmarket" in result.stderr
    assert calls == []


def test_pbs_job_writes_the_script_next_to_the_input_and_runs_it(tmp_path):
    model = PipelineConfiguration.from_mapping(
        {
            "action": "pbs-from-s3",
            "run_id": "r1",
            "stages": {"analysis": True},
            "artifacts": {
                "backend": "s3",
                "uri": "s3://bucket/runs",
                "endpoint_url": "https://s3.example",
                "credentials_file": "/tmp/credentials",
                "profile": "test",
            },
            "coinjoin_analysis": {
                "program": "coinjoin_statistics",
                "data_mode": "mainnet",
                "actions": ["wasabi2", "joinmarket"],
                "input_uri": "s3://bucket/dumplings/generations/g1",
            },
        }
    )
    model.validate()
    script = render_coinjoin_tools_s3_pbs(
        S3Target.from_args(model), "docker://test/image:latest", model.coinjoin_analysis
    )
    assert f"cat > \"$OUTPUT/coinjoin_statistics.sh\" <<'COINJOIN_WORKFLOW'\n{SCRIPT.read_text()}" in script
    assert "cd /work && bash /work/coinjoin_statistics.sh wasabi2 joinmarket" in script
    assert "s3://bucket/dumplings/generations/g1/*" in script
    assert "coinjoin-analysis-tools_data/coinjoin_statistics/$RESULT_ID/" in script
    path = tmp_path / "job.sh"
    path.write_text(script)
    subprocess.run(["bash", "-n", str(path)], check=True)


def test_local_run_copies_the_workflow_script(monkeypatch, tmp_path):
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "Wasabi2CoinJoins.txt").write_text("")
    model = PipelineConfiguration.from_mapping(
        {
            "action": "coinjoin-analysis",
            "run_dir": "r1",
            "coinjoin_analysis": {
                "program": "coinjoin_statistics",
                "data_mode": "mainnet",
                "actions": ["wasabi2"],
                "input_dir": str(source),
            },
        }
    )
    model.validate()
    seen = {}

    def execute(argv, **_kwargs):
        work = Path(argv[argv.index("--volume") + 1].removesuffix(":/work:rw"))
        seen["script"] = (work / "coinjoin_statistics.sh").read_text()
        seen["entrypoint"] = argv[argv.index("--entrypoint") + 1]
        seen["command"] = argv[-2:]

    monkeypatch.setattr(coinjoin_tools.subprocess, "run", execute)
    coinjoin_tools.run_local(model, tmp_path / "r1")
    assert seen["script"] == SCRIPT.read_text()
    assert seen["entrypoint"] == "bash"
    assert seen["command"] == ["/work/coinjoin_statistics.sh", "wasabi2"]
