"""Fast notebook lifecycle checks without BlockSci, SSH or a scheduler."""

import ast
import json
import socket
import os
import subprocess
import sys
from types import ModuleType

from coinjoin_pipeline.execution.pbs.templates import blocksci_notebook
from coinjoin_pipeline.execution.pbs.analysis_modes import prepare_analysis_mode


def test_notebook_paths_use_submission_home_when_pbs_changes_home(tmp_path):
    mode = prepare_analysis_mode(
        "blocksci-notebook", user_script=None, external_baseline_uri=None,
        external_false_cjtxs_uri=None, notebooks_dir=None, notebook_port=8888,
    )
    control = next(line for line in mode.prepare if line.startswith("NOTEBOOK_CONTROL="))
    script = f"OUTPUT_DIR={mode.output}\n{control}\nprintf '%s\\n' \"$OUTPUT_DIR\" \"$NOTEBOOK_CONTROL\""
    frontend_home = tmp_path / "submission-home"
    node_home = tmp_path / "node-home"
    for submit_home, expected in ((str(frontend_home), frontend_home), ("", node_home)):
        result = subprocess.run(
            ["bash", "-c", script], check=True, capture_output=True, text=True,
            env={**os.environ, "HOME": str(node_home), "PBS_O_HOME": submit_home, "RUN_ID": "r1"},
        )
        assert result.stdout.splitlines() == [
            str(expected / "coinjoin-notebooks/r1"),
            str(expected / ".local/share/coinjoin-pipeline/jupyter/r1"),
        ]


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
