"""Fast notebook lifecycle checks without BlockSci, SSH or a scheduler."""

import ast
import json
import socket
import sys
from types import ModuleType, SimpleNamespace

from coinjoin_pipeline.execution.pbs.templates import blocksci_notebook
from coinjoin_pipeline.execution.pbs.analysis_modes import analysis_mode_settings
from coinjoin_pipeline.execution.pbs.blocksci_jobs import analysis_output, notebook_control


def test_notebook_paths_use_submission_home_when_pbs_changes_home(tmp_path, monkeypatch):
    settings = analysis_mode_settings(
        "blocksci-notebook", user_script=None, external_baseline_uri=None,
        external_false_cjtxs_uri=None, notebooks_dir=None, notebook_port=8888,
    )
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
