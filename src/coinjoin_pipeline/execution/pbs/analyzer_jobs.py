"""The S3 PBS jobs that run coinjoin-analysis, the mappings and the unified report.

None of them reads a BlockSci index; ``blocksci_jobs`` holds those that do.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import cast

from coinjoin_pipeline.execution.pbs.stage import CONTAINER_RUNS_ROOT, CONTAINER_SELECTED_ROOT, Stage, StageFailure
from coinjoin_pipeline.execution.pbs.stage_settings import (
    CoinjoinToolsSettings,
    CommandSettings,
    DumplingsBaselineSettings,
    MappingsSettings,
    UnifiedReportSettings,
)


def unified_report(stage: Stage) -> None:
    """Join the precomputed analyzer outputs into the JSON and Markdown report."""
    settings = cast(UnifiedReportSettings, stage.settings)
    stage.log("downloading lightweight report inputs")
    for name in settings["downloads"]:
        stage.download(name)
    for relative in (
        "blocksci-analysis_data/blocksci_analysis.json",
        "coinjoin-analysis_data/coinjoin_tx_info.json",
        ".pipeline/exporters/unified_report.py",
    ):
        stage.require_file(relative, f"Unified S3 report requires {relative}")
    stage.log("assembling JSON and Markdown from precomputed analyzer outputs")
    stage.container(
        CONTAINER_RUNS_ROOT,
        (
            f"{stage.runs_root}:{CONTAINER_RUNS_ROOT}:rw",
            f"{stage.work / '.pipeline/exporters'}:/mnt/exporters:ro",
        ),
        settings["command"],
    )
    stage.require_file(
        "coinjoinPipeline_data/unified_report.json",
        "Unified S3 report did not produce coinjoinPipeline_data/unified_report.json",
    )
    stage.upload(stage.work / "coinjoinPipeline_data", "coinjoinPipeline_data")
    stage.log("report upload complete")


def coinjoin_analysis(stage: Stage) -> None:
    """Run the coinjoin-analysis baseline over the emulator's wallet and coordinator logs.

    The collector scans every run below its root, so the container sees a root that
    holds this run alone: the output directory, with the emulator data read-only in it.
    """
    settings = cast(CommandSettings, stage.settings)
    stage.download("coinjoin_emulator_data")
    data = stage.work / "coinjoin_emulator_data/data"
    if not data.is_dir():
        raise StageFailure("Coinjoin analysis requires coinjoin_emulator_data/data")
    output = stage.work / "coinjoin-analysis_data"
    output.mkdir(parents=True, exist_ok=True)
    selected = stage.scratch / "coinjoin-analysis-selected"
    (selected / stage.run_id).mkdir(parents=True, exist_ok=True)
    run_root = f"{CONTAINER_SELECTED_ROOT}/{stage.run_id}"
    stage.container(
        CONTAINER_SELECTED_ROOT,
        (
            f"{selected}:{CONTAINER_SELECTED_ROOT}:rw",
            f"{output}:{run_root}:rw",
            f"{data}:{run_root}/data:ro",
        ),
        settings["command"],
    )
    stage.require_file(
        "coinjoin-analysis_data/coinjoin_tx_info.json",
        "Coinjoin analysis did not produce coinjoin-analysis_data/coinjoin_tx_info.json",
    )
    stage.upload(output, "coinjoin-analysis_data")


def coinjoin_tools(stage: Stage) -> None:
    """One invocation of a shipped coinjoin-analysis CLI on a copy of its input.

    Every file it leaves, its log and invocation included, is published under its own
    result directory even when the analyzer fails; the comparison baseline is untouched.
    """
    settings = cast(CoinjoinToolsSettings, stage.settings)
    result_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.environ.get('PBS_JOBID') or 'manual'}"
    relative = f"{settings['output_directory']}/{settings['program']}/{result_id}"
    output = stage.work / relative
    (output / "input").mkdir(parents=True)
    stage.finalizers.append(lambda: stage.upload(output, relative))
    if settings.get("input_uri"):
        source = str(settings["input_uri"]).rstrip("/")
        stage.storage.run("sync", f"{source}/*", f"{output / 'input'}/", check=True, timeout=None)
    elif settings["run_input"]:
        experiment = output / "input" / stage.run_id
        experiment.mkdir(parents=True)
        stage.download_optional("coinjoin-analysis_data", experiment)
        stage.download_optional("coinjoin_emulator_data/data", experiment / "data")
        if not any(path.is_file() for path in experiment.rglob("*")):
            raise StageFailure("The run has no baseline or emulator data; provide analysis_input_uri")
    (output / "invocation.json").write_text(json.dumps(settings["invocation"], indent=2) + "\n")
    for name, content in settings["files"].items():
        (output / name).write_text(content)
    stage.singularity(
        settings["command"],
        binds=(f"{output}:/work:rw",),
        environment=("MPLBACKEND=Agg", "MPLCONFIGDIR=/tmp/matplotlib"),
        cleanenv=True,
        log=output / "analysis.log",
    )


def dumplings_baseline(stage: Stage) -> None:
    """The mainnet baseline: coinjoin-analysis over the Dumplings copy mainnet-sync publishes.

    ``manifest.json``, written last, names the copy's height and file sizes; a copy
    without it is being replaced or its upload failed.
    """
    settings = cast(DumplingsBaselineSettings, stage.settings)
    uri, mix_id = settings["dumplings_uri"], settings["mix_id"]
    output = stage.work / "coinjoin-analysis_data"
    output.mkdir(parents=True, exist_ok=True)
    stage.log(f"reading the Dumplings copy from {uri}")
    manifest_path = stage.work / "dumplings-manifest.json"
    stage.storage.copy(f"{uri}/manifest.json", str(manifest_path))
    manifest = json.loads(manifest_path.read_text())
    height = manifest["last_processed_height"]
    stage.log(f"Dumplings Scanner/ at height {height}")
    # Parent of Scanner/: parse_dumplings also writes summary.log and operation.txt one level up.
    work = stage.scratch / "dumplings-work"
    scanner = work / "dumplings/Scanner"
    scanner.mkdir(parents=True)
    stage.storage.copy(f"{uri}/Scanner/*", f"{scanner}/", timeout=None)
    sizes = {
        os.path.relpath(os.path.join(directory, name), scanner): os.path.getsize(os.path.join(directory, name))
        for directory, _, names in os.walk(scanner)
        for name in names
    }
    if sizes != manifest["files"]:
        raise StageFailure("the downloaded Scanner/ does not match its manifest")
    # coinjoin-analysis reads <file>.json instead of a Dumplings file when one exists and
    # never checks its age, so the snapshot must arrive without them.
    for cached in scanner.glob("*.txt.json"):
        cached.unlink()
    stage.singularity(
        f"cd /work && {settings['command']}",
        binds=(f"{work}:/work:rw",),
        environment=("MPLCONFIGDIR=/tmp",),
        cleanenv=True,
    )
    baseline = scanner / mix_id / "coinjoin_tx_info.json"
    if not baseline.is_file():
        raise StageFailure(f"coinjoin-analysis did not produce Scanner/{mix_id}/coinjoin_tx_info.json")
    shutil.copyfile(baseline, output / "coinjoin_tx_info.json")
    # The curated false positives shipped with this coinjoin-analysis image, for the comparison.
    false_cjtxs = output / "false_cjtxs.json"
    curated = f"/opt/coinjoin-analysis/data/{settings['false_cjtxs']}/false_cjtxs.json"
    with false_cjtxs.open("wb") as handle:
        copied = subprocess.run(stage.image_command("cat", curated, cleanenv=True), stdout=handle)
    if copied.returncode:
        print(f"[{stage.name}] the image has no curated false_cjtxs.json", file=sys.stderr)
        false_cjtxs.unlink()
    shutil.copyfile(manifest_path, stage.work / settings["snapshot_file"])
    coinjoins = len(json.loads((output / "coinjoin_tx_info.json").read_text())["coinjoins"])
    stage.log(f"baseline of {coinjoins} coinjoins through height {height}")
    stage.upload(output, "coinjoin-analysis_data")


def coinjoin_mappings(stage: Stage) -> None:
    """Wasabi input-output mappings and Sake over the coinjoin-analysis baseline.

    The enumerator and Sake run from ``templates/mappings_run.sh``, which the
    shared-storage job runs too; their output is published even when they fail.
    """
    settings = cast(MappingsSettings, stage.settings)
    output = stage.work / "coinjoin-mappings_data"
    output.mkdir(parents=True, exist_ok=True)
    stage.finalizers.append(lambda: stage.upload(output, "coinjoin-mappings_data"))
    stage.log("downloading coinjoin-analysis input")
    stage.download("coinjoin-analysis_data")
    baseline = "coinjoin-analysis_data/coinjoin_tx_info.json"
    stage.require_file(baseline, f"CoinJoin mappings require {baseline}")
    environment = {
        **os.environ,
        "WORK": str(stage.work),
        "OUT": str(output),
        "INPUT": str(stage.work / baseline),
        "ENUMERATOR_IMAGE": settings["enumerator_image"],
        "SAKE_IMAGE": settings["sake_image"],
    }
    subprocess.run(["bash", "-euo", "pipefail", "-c", settings["script"]], env=environment, check=True)
    stage.log("completed")
