"""Archived index round trips through local fake S3; no parser, scheduler or network."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coinjoin_pipeline.execution.pbs.templates_s3 import (
    _publish_cache,
    _restore_cache,
    render_blocksci_analyze_s3_pbs,
    render_blocksci_parse_s3_pbs,
    render_blocksci_s3_pbs,
)
from coinjoin_pipeline.execution.pbs.scripts import template
from coinjoin_pipeline.storage.s3 import S3Target

FAKE_S5CMD = Path(__file__).resolve().parents[1] / "support/persistent-index"
pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="archive transport needs zstd")


def shell(tmp_path, body, **settings):
    script = (
        "set -euo pipefail\n"
        + template("s3_stage.sh")
        + template("index_archive.sh")
        + """
CACHE_DIR="$RUN_WORK/blocksci-parse_data"
mkdir -p "$CACHE_DIR"
s5() { s5cmd "$@"; }
"""
        + body
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=3,
        env={
            **os.environ,
            "SCRATCHDIR": str(tmp_path / "scratch"),
            "RUN_ID": "source",
            "STAGE": "blocksci",
            "ARTIFACT_URI": "s3://bucket/runs",
            "IMAGE": "docker://blocksci",
            "FAKE_S3": str(tmp_path / "s3"),
            "PATH": str(FAKE_S5CMD) + os.pathsep + os.environ["PATH"],
            **settings,
        },
    )


def publish(tmp_path, **settings):
    body = """
mkdir -p "$RUN_WORK/blocksci_data/parsed/chain"
printf 'existing parsed blocks' > "$RUN_WORK/blocksci_data/parsed/chain/block.dat"
printf '{"chainConfig":{"dataDirectory":"/old/source"}}' > "$RUN_WORK/blocksci_data/config.json"
EXPORTED_MAX_BLOCK=42 MANIFEST_EXTRA=""
""" + _publish_cache("blocksci", "emulator", "bitcoin_regtest", 2)
    return shell(tmp_path, body, **settings)


def restore(tmp_path):
    return shell(
        tmp_path,
        _restore_cache("$CACHE_DIR", "$ARTIFACT_URI/source/blocksci-parse_data", "source", "Source index")
        + '\nprintf used > "$RUN_WORK/consumer-started"',
        RUN_ID="target",
    )


def cache(tmp_path):
    return tmp_path / "s3/bucket/runs/source/blocksci-parse_data"


@pytest.mark.parametrize("no_pipe", ["", "1"])
def test_streamed_archive_and_scratch_fallback_restore_the_same_verified_index(tmp_path, no_pipe):
    result = publish(tmp_path, FAKE_S5CMD_NO_PIPE=no_pipe)
    assert result.returncode == 0, result.stdout + result.stderr
    files = cache(tmp_path)
    assert sorted(p.name for p in files.iterdir()) == [
        "blocksci_data.tar.zst",
        "blocksci_data.tar.zst.sha256",
        "manifest.json",
    ]
    manifest = json.loads((files / "manifest.json").read_text())
    assert manifest["exported_max_block"] == 42
    assert manifest["archive_sha256"] == hashlib.sha256((files / "blocksci_data.tar.zst").read_bytes()).hexdigest()
    original = (tmp_path / "scratch/coinjoin-run/source/blocksci_data/config.json").read_bytes()
    # No large archive or FIFO survives locally, including the fallback upload.
    assert not list((tmp_path / "scratch").rglob("*.tar.zst"))
    assert not list((tmp_path / "scratch").rglob("*.fifo"))
    result = restore(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    restored = tmp_path / "scratch/coinjoin-run/target/blocksci_data"
    assert (restored / "parsed/chain/block.dat").read_bytes() == b"existing parsed blocks"
    assert json.loads((restored / "config.json").read_text())["chainConfig"]["dataDirectory"] == (
        "/runs/emulation/logs/target/blocksci_data/parsed"
    )
    assert not (restored / ".writing").exists()
    assert (tmp_path / "scratch/coinjoin-run/source/blocksci_data/config.json").read_bytes() == original
    assert not list((tmp_path / "scratch").rglob("*.tar.zst"))


@pytest.mark.parametrize("failure", ["digest", "truncated", "metadata", "missing"])
def test_invalid_archive_never_reaches_a_consumer(tmp_path, failure):
    assert publish(tmp_path).returncode == 0
    files = cache(tmp_path)
    packed = files / "blocksci_data.tar.zst"
    if failure == "digest":
        # Valid compressed data with extra bytes still has a different stream checksum.
        manifest = json.loads((files / "manifest.json").read_text())
        manifest["archive_sha256"] = "0" * 64
        (files / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (files / "blocksci_data.tar.zst.sha256").write_text("0" * 64 + "  blocksci_data.tar.zst\n")
    elif failure == "truncated":
        packed.write_bytes(packed.read_bytes()[:10])
    elif failure == "metadata":
        (files / "blocksci_data.tar.zst.sha256").write_text("0" * 64 + "  blocksci_data.tar.zst\n")
    else:
        packed.unlink()
    result = restore(tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "scratch/coinjoin-run/target/consumer-started").exists()
    if failure != "metadata":
        assert (tmp_path / "scratch/coinjoin-run/target/blocksci_data/.writing").exists()
    assert not list((tmp_path / "scratch").rglob("*.fifo"))


def test_failed_upload_never_publishes_a_manifest(tmp_path):
    # A failed replacement must invalidate the previous archive's completion marker too.
    assert publish(tmp_path).returncode == 0
    result = publish(tmp_path, FAKE_S5CMD_FAIL_PIPE="1")
    assert result.returncode != 0
    assert not (cache(tmp_path) / "manifest.json").exists()
    assert not list((tmp_path / "scratch").rglob("*.fifo"))


def test_notebook_source_is_archived_readonly_and_does_not_reparse():
    target = S3Target("s3://bucket/runs", "target", "https://s3.test", "/storage/credentials", "test")
    script = render_blocksci_analyze_s3_pbs(
        target,
        "docker://blocksci",
        "python /mnt/blocksci_notebook.py 8888",
        mode="blocksci-notebook",
        source_run_id="source",
    )
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "CACHE_RUN_ID=source" in script
    assert "/runs/emulation/logs/$RUN_ID/blocksci_data/parsed:ro" in script
    assert '"$ARTIFACT_URI/$CACHE_RUN_ID/blocksci_data/*"' not in script
    assert "worker.py parse" not in script
    assert "blocksci_parser" not in script
    assert "canonicalize_index\n" in script


def test_combined_job_publishes_an_archive_without_downloading_the_previous_index():
    target = S3Target("s3://bucket/runs", "target", "https://s3.test", "/storage/credentials", "test")
    script = render_blocksci_s3_pbs(target, "docker://blocksci", "true", include_report=False)
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert (
        'index_archive_upload "$RUN_WORK" "$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/blocksci_data.tar.zst"' in script
    )
    assert 's5 sync "$RUN_WORK/blocksci_data/"' not in script
    assert 's5 sync "$ARTIFACT_URI/$RUN_ID/*"' not in script
    # The stage header only defines download helpers; the job's work starts after its trap.
    work = script[script.index("trap on_exit EXIT") :]
    assert work.index("require_zstd\n") < work.index('s5 sync "$ARTIFACT_URI/')


def test_combined_report_job_reads_a_nested_datadir_and_only_present_optional_inputs(tmp_path):
    # The emulator may upload the datadir under bitcoin_data/data/; mappings exist, no prior report.
    inputs = tmp_path / "s3/bucket/runs/source"
    for relative in (
        "bitcoin_data/data/regtest/blocks",
        ".pipeline/exporters",
        "coinjoin_emulator_data/data/btc-node",
        "coinjoin-analysis_data",
        "coinjoin-mappings_data",
    ):
        (inputs / relative).mkdir(parents=True)
    (inputs / "coinjoin_emulator_data/data/btc-node/block_7.json").write_text("{}")
    (inputs / "coinjoin-analysis_data/coinjoin_tx_info.json").write_text("{}")
    (inputs / "coinjoin-mappings_data/coinjoin_mappings.json").write_text("{}")
    binary = tmp_path / "bin"
    binary.mkdir()
    singularity = binary / "singularity"
    singularity.write_text(
        "#!/bin/bash\nset -eu\n"
        'work="$SCRATCHDIR/coinjoin-run/source"\n'
        'case "$*" in *"/bitcoin_data/data:/mnt/data:ro"*) ;; *) echo "wrong datadir: $*" >&2; exit 1 ;; esac\n'
        'test -f "$work/coinjoin-mappings_data/coinjoin_mappings.json"\n'
        'test ! -e "$work/coinjoinPipeline_data"\n'
        'mkdir -p "$work/blocksci_data/parsed/chain" "$work/coinjoinPipeline_data"\n'
        'printf index > "$work/blocksci_data/parsed/chain/block.dat"\n'
        'printf \'{"chainConfig":{"dataDirectory":"/old/source"}}\' > "$work/blocksci_data/config.json"\n'
        'printf report > "$work/coinjoinPipeline_data/unified_report.json"\n'
    )
    singularity.chmod(0o755)
    target = S3Target("s3://bucket/runs", "source", "https://s3.test", "/dev/null", "test")
    script = render_blocksci_s3_pbs(target, "docker://blocksci", "true", include_report=True)
    (tmp_path / "scratch").mkdir()
    result = subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        timeout=3,
        env={
            **os.environ,
            "SCRATCHDIR": str(tmp_path / "scratch"),
            "FAKE_S3": str(tmp_path / "s3"),
            "PATH": os.pathsep.join((str(binary), str(FAKE_S5CMD), os.environ["PATH"])),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (inputs / ".pbs/blocksci.done").is_file()
    assert (inputs / "coinjoinPipeline_data/unified_report.json").read_text() == "report"


@pytest.mark.parametrize("workflow", ["combined", "reusable"])
def test_rendered_jobs_publish_the_same_restorable_index_format(tmp_path, workflow):
    # Stand in only for the container payload, with tiny index files. Run the real
    # generated job, S3 transport, archive publication, finalizer and marker handling.
    inputs = tmp_path / "s3/bucket/runs/source"
    for relative in ("bitcoin_data/regtest/blocks", ".pipeline/exporters", "coinjoin_emulator_data/data/btc-node"):
        (inputs / relative).mkdir(parents=True)
    (inputs / "coinjoin_emulator_data/data/btc-node/block_42.json").write_text("{}")
    binary = tmp_path / "bin"
    binary.mkdir()
    singularity = binary / "singularity"
    singularity.write_text(
        "#!/bin/bash\nset -eu\n"
        'work="$SCRATCHDIR/coinjoin-run/source"\n'
        'mkdir -p "$work/blocksci_data/parsed/chain" "$work/blocksci-analysis_data"\n'
        'printf index > "$work/blocksci_data/parsed/chain/block.dat"\n'
        'printf \'{"chainConfig":{"dataDirectory":"/old/source"}}\' > "$work/blocksci_data/config.json"\n'
        'printf analysis > "$work/blocksci-analysis_data/blocksci_analysis.json"\n'
    )
    singularity.chmod(0o755)
    target = S3Target("s3://bucket/runs", "source", "https://s3.test", "/dev/null", "test")
    if workflow == "combined":
        script = render_blocksci_s3_pbs(target, "docker://blocksci", "true", include_report=False, export_analysis=True)
    else:
        script = render_blocksci_parse_s3_pbs(target, "docker://blocksci", "true")
    (tmp_path / "scratch").mkdir()
    result = subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        timeout=3,
        env={
            **os.environ,
            "SCRATCHDIR": str(tmp_path / "scratch"),
            "FAKE_S3": str(tmp_path / "s3"),
            "PATH": os.pathsep.join((str(binary), str(FAKE_S5CMD), os.environ["PATH"])),
        },
    )
    log = inputs / "logs/blocksci-parse.pbs.log"
    assert result.returncode == 0, result.stdout + result.stderr + (log.read_text() if log.exists() else "")
    stage = "blocksci" if workflow == "combined" else "blocksci-parse"
    assert (inputs / f".pbs/{stage}.done").is_file()
    assert not (inputs / "blocksci_data").exists()
    assert json.loads((cache(tmp_path) / "manifest.json").read_text())["exported_max_block"] == 42
    assert restore(tmp_path).returncode == 0
    if workflow == "combined":
        assert (inputs / "blocksci-analysis_data/blocksci_analysis.json").read_text() == "analysis"
