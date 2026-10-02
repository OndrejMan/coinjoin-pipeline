"""Job templates spell run directories out; they must use the layout's names."""

from __future__ import annotations

import re

import exporters.artifact_paths as layout

from coinjoin_pipeline.paths import EXECUTION_ROOT, PIPELINE_ROOT

RUN_DIRECTORIES = {
    value
    for name, value in vars(layout).items()
    if name.endswith("_DIR") and isinstance(value, str) and value.endswith("_data")
}
# Scratch directories and volumes inside a job, not entries of a run directory.
SCRATCH_DIRECTORIES = {"bitcoin_data", "btc_data", "source-blocksci-parse_data"}
DIRECTORY_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z_-]*_data\b")
JOB_SOURCES = (
    *sorted(EXECUTION_ROOT.glob("*.sh")),
    EXECUTION_ROOT / "pbs" / "templates_s3.py",
    EXECUTION_ROOT / "pbs" / "templates_local.py",
    EXECUTION_ROOT / "kubernetes.py",
    *sorted(PIPELINE_ROOT.glob("*.sh")),
    PIPELINE_ROOT / "compose.yaml",
)


def test_job_templates_use_only_layout_directory_names():
    unknown = {
        path.name: sorted(names)
        for path in JOB_SOURCES
        if (names := set(DIRECTORY_TOKEN.findall(path.read_text())) - RUN_DIRECTORIES - SCRATCH_DIRECTORIES)
    }
    assert unknown == {}


def test_a_misspelled_directory_would_be_caught():
    assert set(DIRECTORY_TOKEN.findall('sync "$RUN_WORK/coinjoin_analysis_data/"')) - RUN_DIRECTORIES


def test_in_cluster_preflight_checks_every_required_exporter():
    import json

    from coinjoin_pipeline.execution.kubernetes import render_s3_emulation_resources
    from coinjoin_pipeline.storage.s3 import REQUIRED_EXPORTERS

    resources = json.loads(
        render_s3_emulation_resources(
            namespace="coinjoin",
            run_id="run-1",
            scenario_json="{}",
            engine="wasabi",
            image_prefix="ghcr.io/ondrejman/",
            emulator_image="emulator:latest",
            uploader_image="pipeline:latest",
            artifact_uri="s3://bucket/runs",
            endpoint_url="https://s3.example",
            secret_name="coinjoin-s3",
        )
    )
    job = next(item for item in resources["items"] if item["kind"] == "Job")
    preflight = job["spec"]["template"]["spec"]["initContainers"][0]["command"][-1]
    assert f"for required in {' '.join(REQUIRED_EXPORTERS)}; do" in preflight
