"""What a configuration renders: the settings and scheduler directives of every S3 PBS job.

Each case is loaded as a user would (YAML, defaults, validation) and rendered by the real
renderers in dry-run; nothing is submitted. The result is compared with a stored JSON in
``rendered_jobs/``, so a change in how options travel from the configuration into a job's
``settings.json`` shows up as a diff.

After an intended change, refresh the stored files and review their diff:

    UPDATE_RENDERED_JOBS=1 uv run --extra test pytest tests/pipeline/test_rendered_jobs.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.context import RunContext
from coinjoin_pipeline.execution import s3_submission
from coinjoin_pipeline.execution.pbs import analysis_modes, templates_s3
from coinjoin_pipeline.execution.pbs.scripts import embedded_file
from coinjoin_pipeline.images import IMAGE_ENVIRONMENT, REGISTRY_ENVIRONMENT

REPO = Path(__file__).resolve().parents[2]
STORED = Path(__file__).with_name("rendered_jobs")
# Every example that submits S3 PBS jobs; mainnet-sync and external-analyze render elsewhere.
EXAMPLES = sorted(path.stem for pattern in ("metacentrum-*.yaml", "wasabi-*.yaml") for path in REPO.glob(f"examples/{pattern}"))

S3 = {
    "backend": "s3",
    "uri": "s3://bucket/runs",
    "endpoint_url": "https://s3.example",
    "credentials_file": "/storage/home/user/.aws/credentials",
    "profile": "test",
}
INDEX = "/scratch/multijob-brno/user/blocksci-mainnet"
BACKUP = "s3://bucket/mainnet/blocksci-index"
DUMPLINGS = "s3://bucket/mainnet/dumplings"
BLOCKS = "s3://bucket/mainnet/blocks"
MULTIJOB = {"blocksci": {"multijob": "brno"}}


def index_job(run_id: str, pbs: dict | None = None, **blocksci: object) -> dict:
    """A BlockSci-only pbs-from-s3 job over an existing index."""
    return {
        "action": "pbs-from-s3",
        "run_id": run_id,
        "artifacts": S3,
        "stages": {"blocksci": True},
        "blocksci": {"workflow": "cached", **blocksci},
        **({"pbs": pbs} if pbs else {}),
    }


# Paths the examples leave out: every way a job reaches a persistent index, and the tools job.
CASES: dict[str, dict] = {
    "notebook-over-the-index-in-place": index_job(
        "notebook-in-place",
        MULTIJOB,
        task="notebook",
        index_dir=INDEX,
        dumplings_dir="/scratch/multijob-brno/user/mainnet/dumplings/Scanner",
        notebook_port=9000,
        notebooks_dir="/storage/home/user/notebooks",
    ),
    "notebook-with-the-index-in-its-scratch": index_job(
        "notebook-in-scratch",
        MULTIJOB,
        task="notebook",
        index_dir=INDEX,
        index_backup_uri=BACKUP,
        index_in_scratch=True,
        dumplings_uri=DUMPLINGS,
    ),
    "notebook-restoring-a-kept-index": index_job(
        "notebook-restore",
        MULTIJOB,
        task="notebook",
        index_dir=INDEX,
        index_backup_uri=BACKUP,
        restore_index=True,
        dumplings_uri=DUMPLINGS,
        storage_report_uri="s3://bucket/mainnet/notebook-storage.json",
    ),
    "script-over-the-persistent-index": index_job(
        "script-persistent",
        MULTIJOB,
        task="script",
        script="scripts/blocksci_script_smoke.py",
        index_dir=INDEX,
        pin_height=850000,
        scan_start_height=800000,
        scan_end_height=850001,
    ),
    "external-report-with-false-positives": index_job(
        "external-report",
        MULTIJOB,
        task="external",
        index_dir=INDEX,
        index_backup_uri=BACKUP,
        index_in_scratch=True,
        external_baseline_uri="s3://bucket/dumplings/wasabi2/coinjoin_tx_info.json",
        external_false_cjtxs_uri="s3://bucket/dumplings/wasabi2/false_cjtxs.json",
        pin_height=850000,
    ),
    "parse-into-a-persistent-index": index_job(
        "persistent-parse",
        MULTIJOB,
        workflow="reusable",
        task="parse",
        index_dir=INDEX,
        index_backup_uri=BACKUP,
        bitcoin_blocks_uri=BLOCKS,
        network="bitcoin_regtest",
        max_block=200,
    ),
    "update-of-another-runs-cache": index_job(
        "cache-update",
        task="update",
        cache_source_run_id="cache-parse",
        bitcoin_blocks_uri=BLOCKS,
        network="bitcoin_regtest",
        max_block=300,
    ),
    "coinjoin-analysis-program": {
        "action": "pbs-from-s3",
        "run_id": "tools",
        "artifacts": S3,
        "stages": {"analysis": True},
        "coinjoin_analysis": {"program": "parse_cj_logs", "actions": ["analyze_only"], "env_vars": "DEBUG=False"},
    },
}


@pytest.fixture(autouse=True)
def fixed_environment(monkeypatch, tmp_path):
    """Nothing of this host reaches a rendered job: its environment, checkout path or filesystem."""
    for name in (*IMAGE_ENVIRONMENT.values(), REGISTRY_ENVIRONMENT, "PBS_BITCOIN_DATADIR", "EMULATION_LOGS_DIR"):
        monkeypatch.delenv(name, raising=False)
    # The committed image locks change with every published image; the jobs get fixed names.
    monkeypatch.setenv("COINJOIN_UPLOADER_IMAGE", "ghcr.io/example/uploader:fixed")
    monkeypatch.setenv("COINJOIN_UNIFIED_REPORT_IMAGE", "ghcr.io/example/unified-report:fixed")
    # Relative scenario and script paths of the examples resolve from the checkout.
    monkeypatch.chdir(REPO)
    # Submission probes shared storage, which only a MetaCentrum frontend has.
    monkeypatch.setattr(templates_s3, "_bitcoin_datadir", str)
    monkeypatch.setattr(analysis_modes, "require_storage_path", lambda path: None)


def render(arguments: list[str], tmp_path: Path) -> dict:
    """Stage name -> what the job would run."""
    config = RunContext.prepare(load_configuration([*arguments, "--runs-root", str(tmp_path), "--dry-run"]), "test").config
    scripts: dict[str, str] = {}

    def record(job, _dry_run):
        scripts[job.stage] = job.script

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(s3_submission, "submit_pbs_job", record)
        s3_submission.submit_s3_pbs_graph(config)
    return {stage: describe(script) for stage, script in scripts.items()}


def describe(script: str) -> dict:
    settings = json.loads(embedded_file(script, "settings.json"))
    if "files" in settings:
        # Sources shipped with a tools job: their names matter here, their text does not.
        settings["files"] = sorted(settings["files"])
    return {
        "directives": [line for line in script.splitlines() if line.startswith("#PBS")],
        "shipped_files": re.findall(r'^cat >"\$JOB/([^"]+)" <<', script, flags=re.MULTILINE),
        "settings": settings,
    }


def check(name: str, rendered: dict) -> None:
    text = json.dumps(rendered, indent=2, sort_keys=True).replace(str(REPO), "<repo>") + "\n"
    stored = STORED / f"{name}.json"
    if os.environ.get("UPDATE_RENDERED_JOBS"):
        STORED.mkdir(exist_ok=True)
        stored.write_text(text, encoding="utf-8")
    assert stored.is_file(), f"{stored} is missing; see this module's docstring"
    assert text == stored.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", EXAMPLES)
def test_an_example_renders_the_stored_jobs(name, tmp_path):
    path = REPO / "examples" / f"{name}.yaml"
    # A full-run without a run ID would generate one from the clock.
    run_id = [] if "run_id" in yaml.safe_load(path.read_text(encoding="utf-8")) else ["--run-id", "example-run"]

    check(name, render(["run", str(path), *run_id], tmp_path))


@pytest.mark.parametrize("name", CASES)
def test_a_configuration_renders_the_stored_jobs(name, tmp_path):
    path = tmp_path / "configuration.yaml"
    path.write_text(yaml.safe_dump(CASES[name]), encoding="utf-8")

    check(name, render(["run", str(path)], tmp_path))


def test_every_stored_file_belongs_to_a_case():
    assert {path.stem for path in STORED.glob("*.json")} == {*EXAMPLES, *CASES}
