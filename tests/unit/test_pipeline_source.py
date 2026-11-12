"""research_manifest.json names the pipeline commit that drove the run."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from coinjoin_pipeline import manifest


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True).stdout


def checkout(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "file").write_text("one\n")
    git(tmp_path, "add", "file")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    return tmp_path


def test_a_checkout_records_its_commit_and_state(tmp_path, monkeypatch):
    root = checkout(tmp_path)
    monkeypatch.setattr(manifest, "PROJECT_ROOT", root)
    monkeypatch.setenv("COINJOIN_PIPELINE_GIT_COMMIT", "f" * 40)
    head = git(root, "rev-parse", "HEAD").strip()
    assert manifest.pipeline_source() == {"origin": "checkout", "commit": head, "dirty": False}
    (root / "file").write_text("two\n")
    assert manifest.pipeline_source()["dirty"] is True


def test_a_checkout_git_cannot_read_never_takes_the_images_commit(tmp_path, monkeypatch):
    root = checkout(tmp_path)
    monkeypatch.setattr(manifest, "PROJECT_ROOT", root)
    monkeypatch.setenv("COINJOIN_PIPELINE_GIT_COMMIT", "f" * 40)
    with mock.patch.object(manifest.subprocess, "run", side_effect=FileNotFoundError("git")):
        assert manifest.pipeline_source() == {"origin": "checkout", "commit": None, "dirty": None}


def test_the_image_copy_records_the_commit_it_was_built_from(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("COINJOIN_PIPELINE_GIT_COMMIT", "a" * 40)
    assert manifest.pipeline_source() == {"origin": "image", "commit": "a" * 40, "dirty": False}
    monkeypatch.setenv("COINJOIN_PIPELINE_GIT_COMMIT", "")
    assert manifest.pipeline_source() == {"origin": "unknown", "commit": None, "dirty": None}


def test_the_initial_manifest_carries_the_source(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("COINJOIN_PIPELINE_GIT_COMMIT", raising=False)
    assert manifest.initial_manifest()["pipeline_source"]["origin"] == "unknown"
