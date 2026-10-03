"""Cache publication must depend on the parsed chain, not sidecar height ranges."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from exporters import verify_chain
from coinjoin_pipeline.execution.pbs.templates_s3 import _verify_block_archive_chain
from coinjoin_pipeline.execution.pbs.validation import PBSError

HASH = "a" * 64


def chain(count):
    return [SimpleNamespace(height=height, hash=HASH) for height in range(count)]


@pytest.mark.parametrize("count", [0, 2, 5])
def test_rejects_short_or_overlong_parsed_chain(count):
    # Sidecars can cover 0..3 using G->A1 and disconnected B2->B3.
    # The connected parser result then contains only G and A1.
    with pytest.raises(ValueError, match="Parsed chain has"):
        verify_chain.verify_chain(chain(count), 3, "bitcoin", HASH)


def test_rejects_a_same_length_wrong_fork():
    with pytest.raises(ValueError, match="does not match checkpoint"):
        verify_chain.verify_chain(chain(4), 3, "bitcoin", "b" * 64)


def test_mainnet_requires_a_trusted_target_hash():
    with pytest.raises(ValueError, match="requires an expected block hash"):
        verify_chain.verify_chain(chain(4), 3, "bitcoin")


def test_records_observed_tip_including_genesis():
    assert verify_chain.verify_chain(chain(1), 0, "bitcoin", HASH) == {
        "exported_max_block": 0,
        "exported_block_hash": HASH,
    }


@pytest.mark.parametrize(
    "source",
    [
        {"exported_max_block": 1},
        {"exported_max_block": True, "exported_block_hash": HASH},
        {"exported_max_block": 4, "exported_block_hash": HASH},
    ],
)
def test_update_refuses_unverified_source(source):
    with pytest.raises(ValueError, match="parse it again"):
        verify_chain.verify_chain(chain(4), 3, "bitcoin", HASH, source)


def test_update_must_preserve_source_tip():
    source = {"exported_max_block": 1, "exported_block_hash": "b" * 64}
    with pytest.raises(ValueError, match="does not preserve source cache block 1"):
        verify_chain.verify_chain(chain(4), 3, "bitcoin", HASH, source)
    source["exported_block_hash"] = HASH
    assert verify_chain.verify_chain(chain(4), 3, "bitcoin", HASH, source)["exported_max_block"] == 3


def test_failed_verification_removes_previous_result(tmp_path, monkeypatch):
    output = tmp_path / "verified.json"
    output.write_text('{"exported_max_block": 3}')
    calls = []

    def load(config, limit):
        calls.append((config, limit))
        return chain(2)

    monkeypatch.setitem(sys.modules, "blocksci", SimpleNamespace(Blockchain=load))
    with pytest.raises(ValueError, match="Parsed chain has 2 blocks"):
        verify_chain.main(
            [
                str(tmp_path / "config.json"),
                "--max-block",
                "3",
                "--network",
                "bitcoin",
                "--expected-block-hash",
                HASH,
                "--output",
                str(output),
            ]
        )
    assert calls == [(str(tmp_path / "config.json"), 0)]
    assert not output.exists()


@pytest.mark.parametrize(
    "count,tip_hash,source_hash,accepted",
    [
        (2, HASH, HASH, False),
        (4, "b" * 64, HASH, False),
        (4, HASH, "b" * 64, False),
        (4, HASH, HASH, True),
    ],
)
def test_pbs_publication_barrier_runs_real_verifier_with_fake_binding(
    tmp_path,
    count,
    tip_hash,
    source_hash,
    accepted,
):
    work = tmp_path / "runs" / "run-1"
    (work / "blocksci_data").mkdir(parents=True)
    source_dir = work / "source-blocksci-parse_data"
    source_dir.mkdir()
    (source_dir / "manifest.json").write_text(
        json.dumps(
            {
                "exported_max_block": 1,
                "exported_block_hash": source_hash,
            }
        )
    )
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "blocksci.py").write_text(
        "from types import SimpleNamespace\n"
        "def Blockchain(config, limit):\n"
        "    assert limit == 0\n"
        f"    return [SimpleNamespace(height=h, hash={tip_hash!r}) for h in range({count})]\n"
    )
    # Replace only Apptainer and BlockSci, execute the real CLI and rendered shell.
    (fake / "singularity").write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "image = sys.argv.index('fake')\n"
        "assert sys.argv[image + 1] == 'env'\n"
        "assert sys.argv[image + 2].startswith('PYTHONPATH=')\n"
        "assert sys.argv[image + 3] == '/usr/bin/python3'\n"
        "arguments = sys.argv[sys.argv.index('/mnt/exporters/verify_chain.py'):]\n"
        f"arguments[0] = {str(Path(verify_chain.__file__))!r}\n"
        f"arguments = [a.replace('/runs/emulation/logs', {str(work.parent)!r}) for a in arguments]\n"
        "os.execv(sys.executable, [sys.executable, *arguments])\n"
    )
    (fake / "singularity").chmod(0o755)
    script = (
        "set -euo pipefail\n"
        + _verify_block_archive_chain("bitcoin", HASH, update=True)
        + """
printf '{"exported_max_block": %s%s}\n' "$EXPORTED_MAX_BLOCK" "$MANIFEST_EXTRA" > "$RUN_WORK/published.json"
"""
    )
    result = subprocess.run(
        ["bash"],
        input=script,
        text=True,
        capture_output=True,
        timeout=3,
        env={
            **os.environ,
            "RUNS_ROOT": str(work.parent),
            "RUN_ID": work.name,
            "RUN_WORK": str(work),
            "EXPORTED_MAX_BLOCK": "3",
            "MANIFEST_EXTRA": ', "block_archive_last_file": 0',
            "IMAGE": "fake",
            "PATH": f"{fake}:{os.environ['PATH']}",
            "PYTHONPATH": str(fake),
        },
    )
    assert (result.returncode == 0) is accepted, result.stderr
    published = work / "published.json"
    assert published.exists() is accepted
    if accepted:
        assert json.loads(published.read_text()) == {
            "exported_max_block": 3,
            "exported_block_hash": HASH,
            "block_archive_last_file": 0,
        }


@pytest.mark.parametrize("network,expected", [("bitcoin", None), ("bitcoin", "bad")])
def test_renderer_refuses_missing_or_invalid_mainnet_checkpoint(network, expected):
    with pytest.raises(PBSError, match="hash"):
        _verify_block_archive_chain(network, expected)
