"""Fast notebook lifecycle checks without BlockSci, SSH or a scheduler."""

import ast
import json
import os
import socket
import sys
from types import ModuleType, SimpleNamespace

import pytest

from coinjoin_pipeline.execution.pbs.templates import blocksci_notebook
from coinjoin_pipeline.configuration import BlockSciConfiguration
from coinjoin_pipeline.execution.pbs.analysis_modes import analysis_mode_settings
from coinjoin_pipeline.execution.pbs.blocksci_analysis_jobs import analysis_output, notebook_control, publish_output
from coinjoin_pipeline.storage.s5client import S5Error


def test_notebook_paths_use_submission_home_when_pbs_changes_home(tmp_path, monkeypatch):
    settings = analysis_mode_settings("blocksci-notebook", BlockSciConfiguration())
    stage = SimpleNamespace(settings=settings, run_id="r1", work=tmp_path / "work")
    frontend_home = tmp_path / "submission-home"
    node_home = tmp_path / "node-home"
    monkeypatch.setenv("HOME", str(node_home))
    for submit_home, expected in ((str(frontend_home), frontend_home), ("", node_home)):
        monkeypatch.setenv("PBS_O_HOME", submit_home)
        assert analysis_output(stage, settings["output"]) == expected / "coinjoin-notebooks/r1"
        assert notebook_control(stage) == expected / ".local/share/coinjoin-pipeline/jupyter/r1"


def test_busy_preferred_port_does_not_prevent_a_second_notebook():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        preferred = busy.getsockname()[1]
        actual = blocksci_notebook.available_port(preferred)
        assert actual != preferred
        with socket.socket() as other:
            other.bind(("127.0.0.1", actual))


def test_restart_preserves_user_notebook_and_settings(tmp_path, monkeypatch):
    kernelspec = ModuleType("ipykernel.kernelspec")
    kernelspec.install = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "ipykernel", ModuleType("ipykernel"))
    monkeypatch.setitem(sys.modules, "ipykernel.kernelspec", kernelspec)
    settings = tmp_path / ".jupyter/app"
    blocksci_notebook.prepare(tmp_path, settings)
    starter = tmp_path / "BlockSci.ipynb"
    notebook = json.loads(starter.read_text())
    assert notebook["metadata"]["kernelspec"]["name"] == "blocksci"
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            ast.parse("".join(cell["source"]))
    starter.write_text("user's saved notebook")
    overrides = settings / "overrides.json"
    overrides.write_text("user's settings")
    blocksci_notebook.prepare(tmp_path, settings)
    assert starter.read_text() == "user's saved notebook"
    assert overrides.read_text() == "user's settings"


def test_autorun_target_reads_only_a_notebook_in_the_notebook_directory(tmp_path):
    root, control = tmp_path / "notebooks", tmp_path / "control"
    root.mkdir()
    control.mkdir()
    assert blocksci_notebook.autorun_target(root, control) is None
    (root / "a.ipynb").write_text("{}")
    (control / "autorun.json").write_text(json.dumps({"name": "a.ipynb"}))
    assert blocksci_notebook.autorun_target(root, control) == (root / "a.ipynb", False)
    (control / "autorun.json").write_text(json.dumps({"name": "a.ipynb", "exit": True}))
    assert blocksci_notebook.autorun_target(root, control) == (root / "a.ipynb", True)
    for name in ("missing.ipynb", "../a.ipynb", None):
        (control / "autorun.json").write_text(json.dumps({"name": name}))
        with pytest.raises(RuntimeError, match="missing"):
            blocksci_notebook.autorun_target(root, control)


def test_run_file_runs_a_script_in_its_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "notebooks"
    folder.mkdir()
    (folder / "ok.py").write_text("open('written', 'w').write('1')\n")
    (folder / "bad.py").write_text("raise SystemExit(3)\n")
    assert blocksci_notebook.run_file(folder / "ok.py") == 0
    assert (folder / "written").read_text() == "1"
    assert blocksci_notebook.run_file(folder / "bad.py") == 3


def test_run_file_saves_every_cell_and_stops_at_the_first_error(tmp_path, monkeypatch):
    pytest.importorskip("nbclient")
    nbformat = pytest.importorskip("nbformat")
    monkeypatch.chdir(tmp_path)
    notebook = nbformat.v4.new_notebook()
    notebook.cells = [
        nbformat.v4.new_code_cell("open('first', 'w').write('1')"),
        nbformat.v4.new_code_cell("raise ValueError('broken')"),
        nbformat.v4.new_code_cell("open('third', 'w').write('1')"),
    ]
    path = tmp_path / "n.ipynb"
    nbformat.write(notebook, str(path))
    monkeypatch.setattr(blocksci_notebook, "KERNEL", "python3")
    assert blocksci_notebook.run_file(path) == 1
    assert (tmp_path / "first").exists() and not (tmp_path / "third").exists()
    saved = nbformat.read(str(tmp_path / "n.autorun.ipynb"), as_version=4)
    assert "ValueError" in json.dumps(saved.cells[1].outputs)


class _Bucket:
    """What `s5cmd sync` and `ls` would do, kept in memory: key -> size."""

    def __init__(self, source, prefix):
        self.source, self.prefix, self.objects = source, prefix, {}
        self.skip, self.after_sync = set(), None

    def upload(self, source, name):
        assert source == self.source
        for path in source.rglob("*"):
            relative = path.relative_to(source).as_posix()
            if path.is_file() and relative not in self.skip:
                self.objects[f"{self.prefix}/{name}/{relative}"] = path.stat().st_size
        if self.after_sync:
            self.after_sync()

    def listing(self, pattern):
        assert pattern == f"{self.prefix}/blocksci-notebooks_data/*"
        # s5cmd leaves out the size of an empty object.
        return [{"key": key, **({"size": size} if size else {})} for key, size in self.objects.items()]


def _notebook_job(tmp_path, **output):
    home = tmp_path / "coinjoin-notebooks/r1"
    (home / "results").mkdir(parents=True)
    (home / "analysis.ipynb").write_text('{"cells": []}')
    (home / "results/table.csv").write_text("a,b\n")
    (home / "empty.txt").write_text("")
    bucket = _Bucket(home, "s3://bucket/runs/r1")
    messages = []
    stage = SimpleNamespace(
        run_prefix=bucket.prefix,
        upload=bucket.upload,
        storage=SimpleNamespace(listing=bucket.listing),
        log=messages.append,
    )
    settings = {"relative": "blocksci-notebooks_data", "notebook": True, **output}
    return home, bucket, stage, settings, messages


def test_default_notebook_directory_leaves_home_once_s3_holds_every_file(tmp_path):
    home, bucket, stage, settings, messages = _notebook_job(tmp_path)
    publish_output(stage, home, settings)
    assert sorted(key.rsplit("/blocksci-notebooks_data/", 1)[1] for key in bucket.objects) == [
        "analysis.ipynb",
        "empty.txt",
        "results/table.csv",
    ]
    assert not home.exists()
    # Other runs' notebooks beside it are not touched.
    assert home.parent.is_dir()
    assert "removed" in messages[-1]


def test_notebooks_stay_in_home_whenever_the_copy_in_s3_is_in_doubt(tmp_path):
    def kept(arrange):
        home, bucket, stage, settings, messages = _notebook_job(tmp_path / str(len(reasons)))
        arrange(home, bucket, stage, settings)
        publish_output(stage, home, settings)
        assert (home / "analysis.ipynb").is_file() and (home / "results/table.csv").is_file()
        reasons.append(messages[-1] if messages else "")

    reasons = []
    # An object is missing, or shorter than the file.
    kept(lambda home, bucket, stage, settings: bucket.skip.add("results/table.csv"))
    assert "S3 does not hold results/table.csv" in reasons[-1]

    def truncated(home, bucket, stage, settings):
        bucket.after_sync = lambda: bucket.objects.update(
            {f"{bucket.prefix}/blocksci-notebooks_data/analysis.ipynb": 1}
        )

    kept(truncated)
    assert "S3 does not hold analysis.ipynb" in reasons[-1]

    # Jupyter saved again while the upload ran: same size, later modification time.
    def saved_again(home, bucket, stage, settings):
        path = home / "analysis.ipynb"
        status = path.stat()
        bucket.after_sync = lambda: os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000_000))

    kept(saved_again)
    assert "changed during the upload" in reasons[-1]

    # A new file appeared during the upload.
    kept(lambda home, bucket, stage, settings: setattr(bucket, "after_sync", (home / "late.py").touch))
    assert "changed during the upload" in reasons[-1]

    # The listing fails.
    def no_listing(home, bucket, stage, settings):
        def fail(_pattern):
            raise S5Error("s5cmd ls failed")

        stage.storage.listing = fail

    kept(no_listing)
    assert "could not be checked" in reasons[-1]


def test_a_directory_the_user_named_and_other_outputs_are_never_removed(tmp_path):
    home, bucket, stage, settings, messages = _notebook_job(tmp_path, notebooks_dir=str(tmp_path / "mine"))
    publish_output(stage, home, settings)
    assert (home / "analysis.ipynb").is_file() and bucket.objects and not messages

    work = tmp_path / "work/blocksci-analysis_data"
    work.mkdir(parents=True)
    (work / "blocksci_analysis.json").write_text("{}")
    uploads = []
    stage = SimpleNamespace(
        run_prefix="s3://bucket/runs/r2", upload=lambda source, name: uploads.append((source, name))
    )
    publish_output(stage, work, {"relative": "blocksci-analysis_data"})
    assert uploads == [(work, "blocksci-analysis_data")] and work.is_dir()


def test_a_failed_upload_keeps_the_notebooks_and_fails_the_finalizer(tmp_path):
    home, _bucket, stage, settings, _messages = _notebook_job(tmp_path)

    def fail(_source, _name):
        raise OSError("s5cmd sync failed")

    stage.upload = fail
    with pytest.raises(OSError, match="sync failed"):
        publish_output(stage, home, settings)
    assert (home / "analysis.ipynb").is_file()
